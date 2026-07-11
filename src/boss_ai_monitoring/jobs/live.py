"""Wave 3: wire the Wave-1 hermetic job functions + scheduler to the live `events` table.

Reads go through `store.connect_read_only()` — jobs never opens a write connection. Last-run
status is persisted as an ordinary `events` row (`event_type="job_run"`, `source="jobs"`,
`payload={"name", "status", ...}`) via the `EventWriter` singleton (G5) — matching the exact shape
🖥 web already built its provenance-footer query against (`web/queries.py::get_provenance_footer`,
BL-05/BL-07): pick the newest row per job name with `QUALIFY row_number() OVER (PARTITION BY
json_extract_string(payload, '$.name') ORDER BY ts DESC) = 1`. `agent_name`/`skill_name`/`model`/
`cost_usd` are left `None` so these rows are excluded from `v_cost_events` (cost_usd IS NOT NULL)
and don't skew `v_attribution`'s per-(agent, skill, model) cost numbers — they do still count
toward that view's `(None, None, None)` bucket's `event_count`/`avg_duration_ms`, alongside any
other un-attributed event; flagged in BL-07 for store to consider filtering.

KNOWN LIMITATION (OQ-04, store's to fix): duckdb refuses a `read_only=True` connection to a file
that already has a live read-write connection in the same process — so `connect_read_only()`
cannot currently coexist with a live `get_writer()` singleton, which is exactly how `bam serve`
runs (one process, one writer, concurrent readers). The functions below are correct and fully
tested against a closed-writer-then-read pattern; there is deliberately no one-call
`build_scheduler(settings)` helper wiring `get_writer()` + these callables together, because that
composition cannot be verified end-to-end until OQ-04 is resolved. Once it is, wire:
`JobScheduler.from_settings(settings.jobs, build_live_callables(settings.store.db_path),
persist=lambda r: persist_job_status(writer, r))`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from boss_ai_monitoring.jobs._events import Event
from boss_ai_monitoring.jobs.correction_scan import SessionCorrectionScore, scan_corrections
from boss_ai_monitoring.jobs.drift_check import DEFAULT_THRESHOLD_PCT, DriftAlert, check_drift
from boss_ai_monitoring.jobs.error_classification import ErrorClassificationRow, classify_errors
from boss_ai_monitoring.jobs.scheduler import JobCallable, JobRunResult
from boss_ai_monitoring.store.schema import EVENT_COLUMNS
from boss_ai_monitoring.store.writer import connect_read_only

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import duckdb

    from boss_ai_monitoring.store.writer import EventWriter

JOB_RUN_EVENT_TYPE = "job_run"
JOB_RUN_SOURCE = "jobs"

_SELECT_EVENTS_SQL = f"SELECT {', '.join(EVENT_COLUMNS)} FROM events ORDER BY ts"

_SELECT_LATEST_JOB_RUN_SQL = """
    SELECT payload
    FROM events
    WHERE event_type = ? AND json_extract_string(payload, '$.name') = ?
    QUALIFY row_number() OVER (ORDER BY ts DESC) = 1
"""


def fetch_events(conn: duckdb.DuckDBPyConnection) -> list[Event]:
    """All events, oldest first, with `payload` decoded from its stored JSON text back to a dict."""
    rows = conn.execute(_SELECT_EVENTS_SQL).fetchall()
    events: list[Event] = []
    for row in rows:
        event: dict[str, Any] = dict(zip(EVENT_COLUMNS, row, strict=True))
        raw_payload = event.get("payload")
        event["payload"] = json.loads(raw_payload) if raw_payload else {}
        events.append(event)
    return events


def run_correction_scan(conn: duckdb.DuckDBPyConnection) -> list[SessionCorrectionScore]:
    return scan_corrections(fetch_events(conn))


def run_drift_check(
    conn: duckdb.DuckDBPyConnection,
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
) -> list[DriftAlert]:
    return check_drift(fetch_events(conn), threshold_pct=threshold_pct)


def run_error_classification(conn: duckdb.DuckDBPyConnection) -> list[ErrorClassificationRow]:
    return classify_errors(fetch_events(conn))


def build_live_callables(db_path: Path) -> dict[str, JobCallable]:
    """One name->callable mapping for `JobScheduler.from_settings`.

    Each call opens its own short-lived read-only connection so every job run sees a fresh
    snapshot of `events` and never contends with the single writer (G5).
    """

    def _wrap(fn: Callable[[duckdb.DuckDBPyConnection], object]) -> JobCallable:
        def _call() -> object:
            conn = connect_read_only(db_path)
            try:
                return fn(conn)
            finally:
                conn.close()

        return _call

    return {
        "correction_scan": _wrap(run_correction_scan),
        "drift_check": _wrap(run_drift_check),
        "error_classification": _wrap(run_error_classification),
    }


def persist_job_status(writer: EventWriter, result: JobRunResult) -> None:
    """Persist one `JobRunResult` as an `events` row — the shape web's footer query reads.

    Every run writes a NEW row (`event_id` includes `finished_at`) rather than updating one in
    place, matching the "events over metrics" append-only design (shared.md) and the ranking
    query web already wrote (`QUALIFY row_number() ... ORDER BY ts DESC = 1` picks the latest).
    """
    payload: dict[str, Any] = {
        "name": result.name,
        "status": result.status,
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "error": result.error,
    }
    if result.name == "drift_check" and isinstance(result.result, list):
        payload["alert_count"] = len(result.result)

    duration_ms = int((result.finished_at - result.started_at).total_seconds() * 1000)
    writer.write(
        {
            "event_id": f"job_run:{result.name}:{result.finished_at.isoformat()}",
            "ts": result.finished_at,
            "source": JOB_RUN_SOURCE,
            "event_type": JOB_RUN_EVENT_TYPE,
            "session_id": None,
            "prompt_id": None,
            "request_id": None,
            "model": None,
            "git_sha": None,
            "agent_name": None,
            "skill_name": None,
            "tool_name": None,
            "cost_usd": None,
            "duration_ms": duration_ms,
            "tokens_input": None,
            "tokens_output": None,
            "tokens_cache_read": None,
            "tokens_cache_creation": None,
            "success": result.status == "ok",
            "cwd": None,
            "payload": payload,
        }
    )


def read_job_status(conn: duckdb.DuckDBPyConnection, job_name: str) -> dict[str, Any] | None:
    """The persisted last-run status for `job_name`, or `None` if it has never run."""
    row = conn.execute(_SELECT_LATEST_JOB_RUN_SQL, [JOB_RUN_EVENT_TYPE, job_name]).fetchone()
    if row is None or row[0] is None:
        return None
    return json.loads(row[0])
