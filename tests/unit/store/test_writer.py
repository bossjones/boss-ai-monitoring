"""RED-first tests for store/writer.py against the BL-01 published interface.

Batching, atomic idempotent flush, cursors, the process-wide singleton, and the read-only
connection helper.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from boss_ai_monitoring.store.writer import (
    EventWriter,
    close_writer,
    connect_read_only,
    get_writer,
)

Event = dict[str, Any]
MakeEvent = Callable[..., Event]


def _scalar(conn: duckdb.DuckDBPyConnection, sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


def _peek_count(db_path: Path, table: str = "events") -> int:
    """Open a second same-config connection to observe rows while a writer may still be open.

    DuckDB allows multiple in-process connections to one file as long as their configuration
    matches (both default, non-read-only) — mixing read_only=True in would raise instead.
    """
    conn = duckdb.connect(str(db_path))
    try:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        return row[0]
    finally:
        conn.close()


def test_writer_creates_db_file_and_schema_on_init(db_path: Path) -> None:
    assert not db_path.exists()

    with EventWriter(db_path):
        pass

    assert db_path.exists()
    conn = connect_read_only(db_path)
    try:
        assert _scalar(conn, "SELECT count(*) FROM events") == 0
    finally:
        conn.close()


def test_write_auto_flushes_at_batch_size(db_path: Path, make_event: MakeEvent) -> None:
    with EventWriter(db_path, batch_size=2, flush_interval_ms=60_000) as writer:
        writer.write(make_event("e1"))
        assert _peek_count(db_path) == 0  # below batch_size, still buffered
        writer.write(make_event("e2"))
        assert _peek_count(db_path) == 2  # hit batch_size -> auto-flushed


def test_flush_interval_zero_flushes_on_every_write(db_path: Path, make_event: MakeEvent) -> None:
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=0) as writer:
        writer.write(make_event("t1"))
        assert _peek_count(db_path) == 1


def test_context_manager_flushes_on_exit(db_path: Path, make_event: MakeEvent) -> None:
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        writer.write(make_event("ctx1"))
        assert _peek_count(db_path) == 0  # not yet flushed

    assert _peek_count(db_path) == 1  # __exit__ flushed before closing


def test_flush_returns_rows_written(db_path: Path, make_event: MakeEvent) -> None:
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        writer.write(make_event("f1"))
        writer.write(make_event("f2"))
        assert writer.flush() == 2
        assert writer.flush() == 0  # nothing buffered


def test_write_many_returns_buffered_count(db_path: Path, make_event: MakeEvent) -> None:
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        count = writer.write_many([make_event("a"), make_event("b"), make_event("c")])
        assert count == 3


def test_flush_is_idempotent_across_flushes_on_event_id(
    db_path: Path, make_event: MakeEvent
) -> None:
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        writer.write(make_event("dup"))
        first = writer.flush()
        writer.write(make_event("dup", payload={"replay": True}))
        second = writer.flush()

    assert first == 1
    assert second == 0
    assert _peek_count(db_path) == 1


def test_within_batch_duplicate_event_id_counts_once(db_path: Path, make_event: MakeEvent) -> None:
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        writer.write(make_event("dup2"))
        writer.write(make_event("dup2"))
        written = writer.flush()

    assert written == 1
    assert _peek_count(db_path) == 1


def test_write_persists_all_fields_correctly(db_path: Path, make_event: MakeEvent) -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    event = make_event("full", ts=ts, payload={"foo": "bar"}, agent_name="agentX")

    with EventWriter(db_path, batch_size=1, flush_interval_ms=60_000) as writer:
        writer.write(event)

    conn = connect_read_only(db_path)
    try:
        row = conn.execute(
            "SELECT event_id, ts, agent_name, json_extract_string(payload, '$.foo') "
            "FROM events WHERE event_id = 'full'"
        ).fetchone()
    finally:
        conn.close()

    # ts is stored as a naive TIMESTAMP normalized to UTC wall-clock (schema.py::pin_utc) — a
    # tz-aware UTC input round-trips to the equivalent naive value, not the original aware one.
    assert row == ("full", ts.replace(tzinfo=None), "agentX", "bar")


def test_write_handles_missing_optional_fields(db_path: Path) -> None:
    minimal: Event = {
        "event_id": "min1",
        "ts": datetime.now(UTC),
        "source": "jsonl",
        "event_type": "user_prompt",
    }

    with EventWriter(db_path, batch_size=1, flush_interval_ms=60_000) as writer:
        writer.write(minimal)

    conn = connect_read_only(db_path)
    try:
        row = conn.execute(
            "SELECT session_id, prompt_id, cost_usd, payload FROM events WHERE event_id = 'min1'"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None
    assert row[3] in (None, "{}")


def test_cursor_round_trip(db_path: Path) -> None:
    with EventWriter(db_path) as writer:
        assert writer.get_cursor("jsonl", "session-a") is None
        writer.set_cursor("jsonl", "session-a", "byte-offset-42")
        assert writer.get_cursor("jsonl", "session-a") == "byte-offset-42"
        writer.set_cursor("jsonl", "session-a", "byte-offset-99")
        assert writer.get_cursor("jsonl", "session-a") == "byte-offset-99"


def test_get_writer_returns_process_wide_singleton(settings: Any) -> None:
    w1 = get_writer(settings)
    w2 = get_writer(settings)
    try:
        assert w1 is w2
    finally:
        w1.close()


def test_connect_read_only_cannot_write(db_path: Path, make_event: MakeEvent) -> None:
    with EventWriter(db_path, batch_size=1) as writer:
        writer.write(make_event("ro1"))

    conn = connect_read_only(db_path)
    try:
        assert _scalar(conn, "SELECT count(*) FROM events") == 1
        with pytest.raises(duckdb.Error):
            conn.execute(
                "INSERT INTO events (event_id, ts, source, event_type) "
                "VALUES ('x', now(), 'otlp', 'api_request')"
            )
    finally:
        conn.close()


def test_concurrent_flush_through_singleton_has_no_exceptions_or_data_loss(
    settings: Any,
) -> None:
    """OQ-02: get_writer()'s singleton must be safe for concurrent callers (otlp/jsonl/langsmith
    sharing one connection). flush() must serialize the WHOLE DB round-trip, not just the buffer
    swap — otherwise two threads can both pass the swap and race BEGIN TRANSACTION on the one
    shared connection.
    """
    writer = get_writer(settings)
    n_threads = 8
    events_per_thread = 25
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(thread_id: int) -> None:
        try:
            for i in range(events_per_thread):
                writer.write(
                    {
                        "event_id": f"race-{thread_id}-{i}",
                        "ts": datetime.now(UTC),
                        "source": "otlp",
                        "event_type": "api_request",
                    }
                )
                writer.flush()
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    writer.flush()
    writer.close()

    assert errors == [], f"concurrent flush raised: {errors!r}"

    conn = connect_read_only(settings.store.db_path)
    try:
        total = _scalar(conn, "SELECT count(*) FROM events")
        distinct = _scalar(conn, "SELECT count(DISTINCT event_id) FROM events")
    finally:
        conn.close()

    expected = n_threads * events_per_thread
    assert total == expected
    assert distinct == expected


def test_connect_read_only_coexists_with_a_live_writer(
    settings: Any, make_event: MakeEvent
) -> None:
    """OQ-04: DuckDB refuses a second `duckdb.connect(path, read_only=True)` while a live,
    non-read-only connection to the same file is already open in-process — exactly the situation
    `bam serve` is in from the moment `get_writer()`'s singleton goes live. connect_read_only()
    must stay usable anyway.
    """
    writer = get_writer(settings)
    try:
        writer.write(make_event("live1"))
        writer.flush()

        reader = connect_read_only(settings.store.db_path)
        try:
            assert _scalar(reader, "SELECT count(*) FROM events") == 1
        finally:
            reader.close()
    finally:
        writer.close()


def test_connect_read_only_sees_only_committed_rows_from_live_writer(
    settings: Any, make_event: MakeEvent
) -> None:
    writer = get_writer(settings)
    try:
        writer.write(make_event("committed1"))
        writer.flush()

        reader = connect_read_only(settings.store.db_path)
        try:
            assert _scalar(reader, "SELECT count(*) FROM events") == 1
            # buffered but not yet flushed — must not be visible to the reader
            writer.write(make_event("buffered_only"))
            assert _scalar(reader, "SELECT count(*) FROM events") == 1
            writer.flush()
            assert _scalar(reader, "SELECT count(*) FROM events") == 2
        finally:
            reader.close()
    finally:
        writer.close()


def test_concurrent_reads_during_writes_do_not_corrupt_or_block(
    settings: Any,
) -> None:
    """Readers via connect_read_only() run concurrently with an actively-flushing writer without
    raising and without ever observing a partial/torn row count.
    """
    writer = get_writer(settings)
    n_events = 100
    stop = threading.Event()
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def write_worker() -> None:
        try:
            for i in range(n_events):
                writer.write(
                    {
                        "event_id": f"cw-{i}",
                        "ts": datetime.now(UTC),
                        "source": "otlp",
                        "event_type": "api_request",
                    }
                )
                writer.flush()
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)
        finally:
            stop.set()

    def read_worker() -> None:
        try:
            while not stop.is_set():
                reader = connect_read_only(settings.store.db_path)
                try:
                    count = _scalar(reader, "SELECT count(*) FROM events")
                    assert 0 <= count <= n_events
                finally:
                    reader.close()
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    writer_thread = threading.Thread(target=write_worker)
    reader_threads = [threading.Thread(target=read_worker) for _ in range(4)]

    writer_thread.start()
    for th in reader_threads:
        th.start()
    writer_thread.join()
    for th in reader_threads:
        th.join()

    writer.close()

    assert errors == [], f"concurrent read/write raised: {errors!r}"

    conn = connect_read_only(settings.store.db_path)
    try:
        assert _scalar(conn, "SELECT count(*) FROM events") == n_events
    finally:
        conn.close()


def test_connect_read_only_still_works_when_no_writer_is_live(
    db_path: Path, make_event: MakeEvent
) -> None:
    with EventWriter(db_path, batch_size=1) as writer:
        writer.write(make_event("closed1"))
    # writer is closed and evicted — no live writer for this path anymore

    conn = connect_read_only(db_path)
    try:
        assert _scalar(conn, "SELECT count(*) FROM events") == 1
    finally:
        conn.close()


def test_close_writer_flushes_buffered_events_on_shutdown(
    settings: Any, db_path: Path, make_event: MakeEvent
) -> None:
    """`bam serve` never closed its writer, so whatever was still buffered was silently dropped.

    Combined with the ingest loops advancing their cursors after buffering, those events were lost
    PERMANENTLY — the next boot resumed past them.
    """
    writer = get_writer(settings)
    writer.write_many([make_event("shutdown-1"), make_event("shutdown-2")])  # under batch_size

    assert _peek_count(db_path) == 0, "precondition: still buffered, nothing flushed yet"

    close_writer(settings.store.db_path)

    assert _peek_count(db_path) == 2, "shutdown must flush the buffer before closing"


def test_close_writer_is_a_no_op_when_no_writer_is_live(db_path: Path) -> None:
    """An idle `bam serve` holds no writer and must not create the DB file just to shut down."""
    close_writer(db_path)

    assert not db_path.exists()
