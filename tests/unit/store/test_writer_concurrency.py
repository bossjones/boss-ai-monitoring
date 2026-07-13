"""RED-first tests for the shared-connection race that crashed `bam serve`.

DuckDB parks the pending result ON the connection object, so `execute()` + `fetchone()` is not
atomic. `EventWriter` hands its single connection to three concurrent producers (otlp / jsonl /
langsmith), and two paths used to touch it without the lock:

  * `get_cursor` / `set_cursor` — called from the jsonl scanner's worker thread
    (`scan_once` via `asyncio.to_thread`)
  * `langsmith_poll._known_session_ids` — reached into `writer._conn` from the event loop and ran
    `SELECT DISTINCT session_id FROM events`

Interleaved, `get_cursor().fetchone()` returned the OTHER query's row — a bare session UUID — and
`jsonl.py`'s `int(cursor_raw)` blew up with
`ValueError: invalid literal for int() with base 10: 'f18ed300-...'`.

The fix is two-sided (locking the cursor accessors alone is not enough), so these tests assert the
invariant from both ends: the behaviour under concurrency, and the structural rule that nothing
outside `store/writer.py` may touch the private connection.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from boss_ai_monitoring.ingest.langsmith_poll import _known_session_ids
from boss_ai_monitoring.store.writer import EventWriter, WriterClosedError

Event = dict[str, Any]
MakeEvent = Callable[..., Event]

_CURSOR_KEY = "/transcripts/a.jsonl"
_CURSOR_VALUE = "42"


def test_get_cursor_is_not_corrupted_by_a_concurrent_reader_on_the_writer_connection(
    db_path: Path, make_event: MakeEvent
) -> None:
    """The exact production shape: the langsmith poller reading `events` while the jsonl scanner
    reads its byte-offset cursor, both against the one writer connection.

    Asserts on the VALUE, not on `pytest.raises(ValueError)`: the corruption can also hand back a
    valid-but-foreign row or `None`, and those are silent data loss rather than a crash. Comparing
    against what we wrote catches all three.
    """
    writer = EventWriter(db_path)
    # Many distinct session_ids so `SELECT DISTINCT session_id` spans several thread switches —
    # this widens the interleaving window from a needle to a barn door.
    writer.write_many([make_event(f"seed-{i}", session_id=f"sess-{i:05d}") for i in range(5000)])
    writer.flush()
    writer.set_cursor("jsonl", _CURSOR_KEY, _CURSOR_VALUE)

    corrupted: list[Any] = []
    errors: list[BaseException] = []
    stop = threading.Event()
    old_switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # force frequent hand-offs between the two threads

    def langsmith_reader() -> None:
        try:
            while not stop.is_set():
                _known_session_ids(writer)
        except BaseException as exc:
            errors.append(exc)

    def jsonl_cursor_reader() -> None:
        try:
            for _ in range(2000):
                value = writer.get_cursor("jsonl", _CURSOR_KEY)
                if value != _CURSOR_VALUE:
                    corrupted.append(value)
                    return
        except BaseException as exc:
            errors.append(exc)
        finally:
            stop.set()

    threads = [
        threading.Thread(target=langsmith_reader),
        threading.Thread(target=jsonl_cursor_reader),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        # Doubles as the deadlock guard: `self._lock` is non-reentrant, so if anyone ever calls a
        # lock-taking public method from inside a lock-holding region, this hangs instead of racing.
        assert not any(t.is_alive() for t in threads), "a thread never finished — deadlock?"
    finally:
        sys.setswitchinterval(old_switch_interval)
        stop.set()
        writer.close()

    assert errors == [], f"concurrent cursor read raised: {errors!r}"
    assert corrupted == [], f"get_cursor() returned another query's row: {corrupted!r}"


def test_the_writer_connection_is_private_and_has_a_public_read_door(db_path: Path) -> None:
    """`_conn` must not be reachable from outside; `cursor()` is the supported way to read.

    A cursor has its OWN result set, so a caller can never steal the writer's pending rows — and,
    unlike taking the write lock, a full-table scan through it does not stall an in-flight flush.
    """
    with EventWriter(db_path) as writer:
        assert not hasattr(writer, "_conn"), "the writer's connection must be name-mangled private"

        read_cursor = writer.cursor()
        try:
            assert read_cursor.execute("SELECT count(*) FROM events").fetchone() == (0,)
        finally:
            read_cursor.close()


def test_no_module_outside_the_writer_reaches_into_the_private_connection() -> None:
    """The regression guard. `langsmith_poll` borrowed `writer._conn` because `connect_read_only`
    had already set the precedent — so ban the pattern outright rather than trusting review.
    """
    src = Path(__file__).resolve().parents[3] / "src" / "boss_ai_monitoring"
    writer_module = src / "store" / "writer.py"

    offenders = [
        path.relative_to(src).as_posix()
        for path in sorted(src.rglob("*.py"))
        if path != writer_module and "._conn" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], (
        f"these modules reach into EventWriter's private connection: {offenders}. "
        "Use writer.cursor() for reads, or add a locked accessor on EventWriter."
    )


def test_using_a_closed_writer_raises_instead_of_silently_dropping_the_event(
    db_path: Path, make_event: MakeEvent
) -> None:
    """A post-close `write()` used to append to the buffer and return success — the event was
    simply gone, with no error anywhere. Shutdown now closes the writer (`close_writer`), so this
    is reachable: a worker thread can still be inside `scan_once` when the server tears down
    (cancelling the task does NOT stop its `asyncio.to_thread` thread).
    """
    writer = EventWriter(db_path)
    writer.close()

    with pytest.raises(WriterClosedError):
        writer.write(make_event("after-close"))

    with pytest.raises(WriterClosedError):
        writer.write_many([make_event("after-close-many")])

    with pytest.raises(WriterClosedError):
        writer.get_cursor("jsonl", "/some/path.jsonl")

    with pytest.raises(WriterClosedError):
        writer.cursor()


def test_close_is_idempotent(db_path: Path) -> None:
    """Shutdown may close a writer that a `with` block already closed. That must be a no-op."""
    writer = EventWriter(db_path)
    writer.close()
    writer.close()  # must not raise
