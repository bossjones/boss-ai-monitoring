"""RED-first tests for ingest/jsonl.py (Phase 4: JSONL transcript backfill + gap-fill).

Pure pytest over fixture directories in tmp_path — hermetic, no real ~/.claude/projects access.
Fixtures live in tests/fixtures/jsonl/ and mirror the empirically-observed real transcript shape
(see OQ-jsonl-01 in .team/boss-ai-monitoring-build.open-questions.md for what was reverse
engineered and why).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest

from boss_ai_monitoring.ingest.jsonl import ScanStats, run_forever, scan_once
from boss_ai_monitoring.store.writer import EventWriter


def _peek_rows(db_path: Path, *, where: str = "1=1") -> list[dict[str, Any]]:
    """Open a second connection to observe rows while the writer may still be open."""
    conn = duckdb.connect(str(db_path))
    try:
        cols = [d[0] for d in conn.execute("SELECT * FROM events LIMIT 0").description]
        rows = conn.execute(f"SELECT * FROM events WHERE {where}").fetchall()
        return [dict(zip(cols, row, strict=True)) for row in rows]
    finally:
        conn.close()


def _peek_count(db_path: Path, *, where: str = "1=1") -> int:
    conn = duckdb.connect(str(db_path))
    try:
        row = conn.execute(f"SELECT count(*) FROM events WHERE {where}").fetchone()
        assert row is not None
        return row[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Discovery + parsing: completed session
# ---------------------------------------------------------------------------


def test_scan_discovers_files_and_parses_completed_session(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    copy_fixture("completed_session.jsonl", projects_dir / "proj-a")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        stats = scan_once(projects_dir, writer)
        writer.flush()

    assert stats.files_scanned == 1
    assert stats.events_written > 0

    rows = _peek_rows(db_path, where="source = 'jsonl'")
    assert rows
    event_types = {r["event_type"] for r in rows}
    assert "user_prompt" in event_types
    assert "api_request" in event_types
    assert "tool_result" in event_types

    session_id = "11111111-1111-1111-1111-111111111111"
    for row in rows:
        assert row["session_id"] == session_id
        assert row["source"] == "jsonl"


def test_scan_populates_request_id_on_api_request_events(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    copy_fixture("completed_session.jsonl", projects_dir / "proj-a")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        scan_once(projects_dir, writer)
        writer.flush()

    rows = _peek_rows(db_path, where="event_type = 'api_request'")
    assert {r["request_id"] for r in rows} == {"req-001", "req-002"}


def test_scan_extracts_cwd_from_transcript_metadata(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    copy_fixture("completed_session.jsonl", projects_dir / "proj-a")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        scan_once(projects_dir, writer)
        writer.flush()

    rows = _peek_rows(db_path, where="source = 'jsonl'")
    assert all(r["cwd"] == "/Users/example/dev/example-repo" for r in rows)


def test_scan_tracks_prompt_id_across_lines_in_one_task(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    copy_fixture("completed_session.jsonl", projects_dir / "proj-a")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        scan_once(projects_dir, writer)
        writer.flush()

    rows = _peek_rows(db_path, where="source = 'jsonl'")
    prompt_id = "aaaaaaaa-0000-0000-0000-000000000001"
    # Assistant lines don't carry promptId directly in real transcripts; the scanner must
    # carry the most-recently-seen prompt_id forward within the session.
    assert all(r["prompt_id"] == prompt_id for r in rows)


# ---------------------------------------------------------------------------
# Cursor: incremental delta on a still-growing session
# ---------------------------------------------------------------------------


def test_second_scan_ingests_only_the_delta(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    path = copy_fixture("growing_session.jsonl", projects_dir / "proj-b")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        first = scan_once(projects_dir, writer)
        writer.flush()
        first_count = _peek_count(db_path, where="source = 'jsonl'")
        assert first.events_written == first_count
        assert first_count > 0

        # simulate the session growing: append the "next turn" onto the same file
        appended = Path("tests/fixtures/jsonl/growing_session.append.jsonl").read_text()
        with path.open("a") as f:
            f.write(appended)

        second = scan_once(projects_dir, writer)
        writer.flush()
        second_count = _peek_count(db_path, where="source = 'jsonl'")

    assert second.events_written > 0
    assert second_count == first_count + second.events_written

    # a third scan with nothing appended ingests nothing new
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        third = scan_once(projects_dir, writer)
        writer.flush()

    assert third.events_written == 0


def test_cursor_persists_byte_offset_per_file(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    path = copy_fixture("completed_session.jsonl", projects_dir / "proj-a")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        scan_once(projects_dir, writer)
        cursor = writer.get_cursor("jsonl", str(path))

    assert cursor is not None
    assert int(cursor) == path.stat().st_size


def test_a_corrupt_non_integer_cursor_rescans_the_file_instead_of_crashing(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    """A GUARD, not the fix for OQ-06.

    The shared-connection race (see tests/unit/store/test_writer_concurrency.py) is what actually
    put a session UUID where a byte offset belonged and killed the scan pass with
    `ValueError: invalid literal for int() with base 10: 'f18ed300-...'`. That is fixed by locking
    the cursor accessors. This guard only covers a genuinely corrupt `ingest_cursors` row — a
    hand-edited DB, a restored snapshot, a future schema change — and it is safe to rescan because
    the flush anti-joins on `event_id`, so re-ingest writes zero duplicate rows.
    """
    path = copy_fixture("completed_session.jsonl", projects_dir / "proj-a")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        writer.set_cursor("jsonl", str(path), "f18ed300-bc85-4b9f-918f-845d4bc5140c")

        stats = scan_once(projects_dir, writer)  # must not raise
        writer.flush()

        assert stats.events_written > 0, "a corrupt cursor should rescan the file from byte 0"
        assert writer.get_cursor("jsonl", str(path)) == str(path.stat().st_size)


def test_truncated_file_resets_cursor_safely(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    path = copy_fixture("completed_session.jsonl", projects_dir / "proj-a")

    original_size = path.stat().st_size

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        scan_once(projects_dir, writer)
        writer.flush()
        full_count = _peek_count(db_path, where="source = 'jsonl'")
        assert full_count > 0

        # truncate + rewrite with fresh content and a NEW session_id so re-ingestion is visible
        rotated_session = "77777777-7777-7777-7777-777777777777"
        rotated = {
            "type": "user",
            "uuid": "ru1",
            "parentUuid": None,
            "sessionId": rotated_session,
            "promptId": "bbbb0000-0000-0000-0000-000000000001",
            "timestamp": "2026-07-11T09:00:00.000Z",
            "cwd": "/Users/example/dev/rotated-repo",
            "gitBranch": "main",
            "isSidechain": False,
            "userType": "external",
            "version": "2.1.0",
            "message": {"role": "user", "content": "fresh content after rotation"},
        }
        path.write_text(json.dumps(rotated) + "\n")
        assert path.stat().st_size < original_size  # sanity: file is now much smaller

        stats = scan_once(projects_dir, writer)
        writer.flush()

    assert stats.events_written >= 1
    rows = _peek_rows(db_path, where=f"session_id = '{rotated_session}'")
    assert rows


# ---------------------------------------------------------------------------
# Malformed lines never crash the scan
# ---------------------------------------------------------------------------


def test_malformed_line_is_skipped_and_logged(
    projects_dir: Path,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
    copy_fixture: Callable[..., Path],
) -> None:
    copy_fixture("malformed_line.jsonl", projects_dir / "proj-c")

    with (
        caplog.at_level("WARNING"),
        EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer,
    ):
        stats = scan_once(projects_dir, writer)
        writer.flush()

    # both valid lines (a user prompt and an assistant reply) still got ingested
    assert stats.events_written == 2
    assert any("malformed" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Subagents (sidechains) are still captured
# ---------------------------------------------------------------------------


def test_subagent_sidechain_events_are_captured(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    copy_fixture("session_with_subagent.jsonl", projects_dir / "proj-d")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        scan_once(projects_dir, writer)
        writer.flush()

    session_id = "33333333-3333-3333-3333-333333333333"
    rows = _peek_rows(db_path, where=f"session_id = '{session_id}'")
    assert rows
    # the sidechain's tool_result must be present, tagged the same session
    tool_results = [r for r in rows if r["event_type"] == "tool_result"]
    assert tool_results
    for row in rows:
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        assert payload.get("sessionId") == session_id


# ---------------------------------------------------------------------------
# Compaction boundary
# ---------------------------------------------------------------------------


def test_session_spanning_compaction_emits_compaction_event(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    copy_fixture("compaction_session.jsonl", projects_dir / "proj-e")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        scan_once(projects_dir, writer)
        writer.flush()

    rows = _peek_rows(db_path, where="event_type = 'compaction'")
    assert len(rows) == 1

    prompt_id = "aaaaaaaa-0000-0000-0000-000000000006"
    all_rows = _peek_rows(db_path, where="session_id = '66666666-6666-6666-6666-666666666666'")
    # prompt_id carries across the compaction boundary — it's still one task.
    assert all(r["prompt_id"] == prompt_id for r in all_rows)


# ---------------------------------------------------------------------------
# Unicode / emoji content
# ---------------------------------------------------------------------------


def test_unicode_and_emoji_content_round_trips(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    copy_fixture("unicode_emoji.jsonl", projects_dir / "proj-f")

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        scan_once(projects_dir, writer)
        writer.flush()

    rows = _peek_rows(db_path, where="event_type = 'user_prompt'")
    assert rows
    payload = (
        json.loads(rows[0]["payload"])
        if isinstance(rows[0]["payload"], str)
        else rows[0]["payload"]
    )
    content = payload["message"]["content"]
    assert "🐛" in content
    assert "日本語" in content


# ---------------------------------------------------------------------------
# Empty projects dir
# ---------------------------------------------------------------------------


def test_empty_projects_dir_returns_zero_stats(tmp_path: Path, db_path: Path) -> None:
    empty_dir = tmp_path / "empty-projects"
    empty_dir.mkdir()

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        stats = scan_once(empty_dir, writer)

    assert stats == ScanStats(files_scanned=0, events_written=0)


def test_missing_projects_dir_does_not_crash(tmp_path: Path, db_path: Path) -> None:
    missing_dir = tmp_path / "does-not-exist"

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        stats = scan_once(missing_dir, writer)

    assert stats.files_scanned == 0
    assert stats.events_written == 0


# ---------------------------------------------------------------------------
# Scan stays under a time budget with thousands of files
# ---------------------------------------------------------------------------


def test_scan_thousands_of_files_stays_under_time_budget(projects_dir: Path, db_path: Path) -> None:
    fixture_text = (Path("tests/fixtures/jsonl") / "completed_session.jsonl").read_text()
    n_files = 3000
    for i in range(n_files):
        subdir = projects_dir / f"proj-{i}"
        subdir.mkdir()
        # give each file a distinct sessionId so events don't collide
        text = fixture_text.replace("11111111-1111-1111-1111-111111111111", f"gen-session-{i:05d}")
        (subdir / "session.jsonl").write_text(text)

    with EventWriter(db_path, batch_size=5000, flush_interval_ms=60_000) as writer:
        started = time.monotonic()
        stats = scan_once(projects_dir, writer)
        elapsed = time.monotonic() - started
        writer.flush()

    assert stats.files_scanned == n_files
    assert elapsed < 20.0  # generous budget for 3000 tiny files; catches quadratic blowups


# ---------------------------------------------------------------------------
# run_forever: the polling loop wrapper
# ---------------------------------------------------------------------------


async def test_run_forever_scans_once_per_iteration(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    copy_fixture("completed_session.jsonl", projects_dir / "proj-a")
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        await run_forever(projects_dir, writer, interval_s=15, iterations=2, sleep=fake_sleep)
        writer.flush()

    # 2 iterations, 1 sleep between them — the loop never sleeps after the final pass.
    assert sleeps == [15]
    rows = _peek_rows(db_path, where="source = 'jsonl'")
    assert rows  # the single scan pass (first iteration) already ingested everything


async def test_run_forever_survives_a_failing_pass(
    tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import boss_ai_monitoring.ingest.jsonl as jsonl_module

    def _boom(_projects_dir: Path, _writer: EventWriter) -> ScanStats:
        raise RuntimeError("boom")

    monkeypatch.setattr(jsonl_module, "scan_once", _boom)

    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        # must not raise despite scan_once always failing
        await run_forever(
            tmp_path / "projects", writer, interval_s=1, iterations=3, sleep=fake_sleep
        )

    assert calls == [1, 1]


@pytest.mark.asyncio
async def test_scan_does_not_block_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """`scan_once` is synchronous and, over a real ~/.claude/projects, takes MINUTES.

    Awaited directly on the loop it freezes the whole app — dashboard, OTLP receiver and all —
    while the first backfill runs. Found live: a fresh `bam serve` grew a 525MB database while
    every HTTP request hung. It must run off the loop.
    """
    import asyncio

    import boss_ai_monitoring.ingest.jsonl as jsonl_module

    ticks = 0
    ticks_seen_during_scan = -1

    async def heartbeat() -> None:
        nonlocal ticks
        for _ in range(40):
            await asyncio.sleep(0.01)
            ticks += 1

    def slow_blocking_scan(*_args: Any, **_kwargs: Any) -> ScanStats:
        nonlocal ticks_seen_during_scan
        time.sleep(0.25)  # stands in for a real multi-minute backfill
        # The count AT THE END OF THE SCAN is the whole test: if the scan ran on the event loop,
        # the heartbeat could not have advanced at all while we slept here.
        ticks_seen_during_scan = ticks
        return ScanStats(files_scanned=0, events_written=0)

    monkeypatch.setattr(jsonl_module, "scan_once", slow_blocking_scan)

    await asyncio.gather(
        run_forever(tmp_path, None, interval_s=0, iterations=1),  # type: ignore[arg-type]
        heartbeat(),
    )

    assert ticks_seen_during_scan > 0, (
        "the event loop was frozen for the whole scan — no other coroutine could run"
    )


def test_scan_does_not_advance_the_cursor_past_events_it_never_flushed(
    projects_dir: Path, db_path: Path, copy_fixture: Callable[..., Path]
) -> None:
    """Cursor durability must FOLLOW data durability, or a restart loses events permanently.

    `write_many()` only flushes at `batch_size` (500) — it ignores `flush_interval_ms`, there is no
    periodic flusher, and `bam serve` never closes its writer. So a scan pass that produces fewer
    than 500 events buffers them in memory and then records the cursor as having CONSUMED them.
    Ctrl-C and they are gone: the next boot resumes from the advanced offset and never re-reads
    them.
    """
    path = copy_fixture("completed_session.jsonl", projects_dir / "proj-a")

    # deliberately NOT a `with` block — `bam serve` never closes the writer, and that is the point
    writer = EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000)
    stats = scan_once(projects_dir, writer)
    cursor = writer.get_cursor("jsonl", str(path))

    assert stats.events_written > 0, "fixture must produce events for this test to mean anything"
    assert _peek_count(db_path) == stats.events_written, (
        "events were buffered but never flushed, yet the cursor advanced past them — "
        "a restart would lose them forever"
    )
    assert cursor == str(path.stat().st_size)
