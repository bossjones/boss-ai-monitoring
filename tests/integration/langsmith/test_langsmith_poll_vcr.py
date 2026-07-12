"""poll_once against recorded real-API traffic (see conftest.py for the VCR harness).

These tests replay cassettes recorded from api.smith.langchain.com, so they catch contract
drift that respx mocks structurally cannot (page-size caps, envelope changes, cursor shape):
the 2026-07-12 `limit=200 -> 400 Bad Request` bug shipped green precisely because every mock
was hand-built to be compliant. Recording is the moment of truth — a 400 fails the recording
run — and replay pins the accepted wire shape into CI.

Determinism: each test seeds a fixed cursor before polling so the /runs/query request body
is stable across record and replay (a cursorless first poll computes start_time from
datetime.now, which changes every run). FIXED_TS values were chosen at record time against
the real boss-ai-monitoring project.

One deliberate deviation from specs/tdd-vcr-tests.md, forced by recorded reality: the SDK's
list_runs(limit=N) is both the page size and a TOTAL results cap (async_client.py breaks at
`ix >= limit`), so one poll_once pass can never see more than 100 runs and the spec's
single-pass `runs_seen > 100` assertion is unsatisfiable. The drain test asserts the
equivalent production truth across two cursor-chained passes instead. (Recording this suite
also surfaced that /runs/query defaults to newest-first, which made a capped pass drop the
older backlog permanently — fixed by ordering ascending in poll_once; the drain test is the
live regression for both that fix and the original limit-clamp bug.)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from langsmith import AsyncClient

from boss_ai_monitoring.ingest.langsmith_poll import poll_once
from boss_ai_monitoring.store.writer import EventWriter

pytestmark = pytest.mark.langsmith_vcr

PROJECT = "boss-ai-monitoring"
# Narrow window (19 runs at record time: the tail of 2026-07-12's 18:06-18:34 burst) for
# the single-pass and idempotency tests — keeps those cassettes small enough to review.
FIXED_TS = "2026-07-12T18:30:00+00:00"
# Wide window covering the whole burst (>100 runs at record time) for the page-cap test.
FIXED_TS_WIDE = "2026-07-12T00:00:00+00:00"


def _langsmith_rows(db_path: Path) -> list[dict[str, Any]]:
    import duckdb

    conn = duckdb.connect(str(db_path))
    try:
        cols = [d[0] for d in conn.execute("SELECT * FROM events LIMIT 0").description]
        rows = conn.execute("SELECT * FROM events WHERE source = 'langsmith'").fetchall()
        return [dict(zip(cols, row, strict=True)) for row in rows]
    finally:
        conn.close()


async def test_poll_once_against_live_recording(db_path: Path) -> None:
    """One pass over a real recorded window lands real runs in DuckDB and advances the cursor."""
    client = AsyncClient()
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        writer.set_cursor("langsmith", PROJECT, FIXED_TS)
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()
        writer.flush()
        cursor = writer.get_cursor("langsmith", PROJECT)

    assert result.status == "ok"
    assert result.runs_seen > 0
    assert result.events_written == result.runs_seen
    assert cursor is not None
    assert datetime.fromisoformat(cursor) > datetime.fromisoformat(FIXED_TS)

    rows = _langsmith_rows(db_path)
    assert len(rows) == result.events_written
    assert all(str(row["event_id"]).startswith("langsmith:") for row in rows)


async def test_capped_backlog_drains_across_passes(db_path: Path) -> None:
    """Live-shaped regression for the limit bug AND the ordering bug, over >100 real runs.

    poll_once(limit=250) must clamp the page size to the API max of 100 — re-recording with
    the unclamped historical request (limit sent verbatim) would 400 and fail the recording
    run — and must ask for the OLDEST runs first, so the pass that fills the cap leaves the
    cursor flooring the remainder. Pass 2 then drains the backlog past 100 distinct runs,
    exactly as run_forever's next poll interval would. (With the API's newest-first default,
    the older backlog was permanently skipped and this test could not pass.)
    """
    client = AsyncClient()
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        writer.set_cursor("langsmith", PROJECT, FIXED_TS_WIDE)
        first = await poll_once(PROJECT, writer, client=client, limit=250)
        second = await poll_once(PROJECT, writer, client=client, limit=250)
        await client.aclose()
        writer.flush()

    assert first.status == "ok"
    assert first.runs_seen == 100  # the full clamped page — the API accepted limit=100
    assert second.status == "ok"
    assert second.runs_seen > 0

    rows = _langsmith_rows(db_path)
    distinct_ids = {row["event_id"] for row in rows}
    assert len(rows) == len(distinct_ids)  # dedupe held across the overlap re-fetch
    assert len(distinct_ids) > 100  # the backlog past the cap DRAINED instead of being lost


async def test_second_pass_is_idempotent_and_cursor_driven(db_path: Path) -> None:
    """A second pass over an already-drained window re-serves only the overlap, without dupes.

    The cursor is persisted 60s behind the latest run (_CURSOR_OVERLAP), so pass 2 re-fetches
    that fringe; the writer's event_id anti-join must make it a no-op, not duplicate rows.
    """
    client = AsyncClient()
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        writer.set_cursor("langsmith", PROJECT, FIXED_TS)
        first = await poll_once(PROJECT, writer, client=client)
        second = await poll_once(PROJECT, writer, client=client)
        await client.aclose()
        writer.flush()

    assert first.status == "ok"
    assert first.runs_seen > 0
    assert second.status == "ok"

    rows = _langsmith_rows(db_path)
    distinct_ids = {row["event_id"] for row in rows}
    assert len(rows) == len(distinct_ids)  # no duplicate event_ids: dedupe held
    # Pass 2 re-served the overlap fringe (and at most any runs that landed mid-recording);
    # every row traces back to a run one of the two passes saw.
    assert first.runs_seen <= len(distinct_ids) <= first.runs_seen + second.runs_seen
