"""RED-FIRST: live wiring (Wave 3) — jobs against the real events table.

Wave 1 hermetic job functions (`correction_scan`, `drift_check`, `error_classification`) operate
on plain fixture rows; this module proves the Wave-3 wiring that reads real rows via
`store.connect_read_only()` and persists last-run status as an `events` row
(`event_type="job_run"`) through an `EventWriter` — jobs NEVER opens a second write connection
(G5); this is the exact shape web's `get_provenance_footer` query already reads (BL-05/BL-07).

NOTE (OQ-04): duckdb refuses `connect_read_only()` while a read-write connection to the same file
is still open in-process, so every test here writes through a short-lived `EventWriter(...)`
context manager (closed before reading) rather than the long-lived `get_writer()` singleton —
see OQ-04 for why the singleton can't be used this way and what it means for `bam serve`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from boss_ai_monitoring.jobs.drift_check import DriftAlert
from boss_ai_monitoring.jobs.live import (
    build_live_callables,
    fetch_events,
    persist_job_status,
    read_job_status,
    run_correction_scan,
    run_drift_check,
    run_error_classification,
)
from boss_ai_monitoring.jobs.scheduler import JobRunResult
from boss_ai_monitoring.store.writer import EventWriter, connect_read_only


def test_fetch_events_round_trips_all_fields_and_decodes_payload(settings, event_factory) -> None:
    event = event_factory(
        event_id="evt-live-1",
        ts=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        source="otlp",
        event_type="api_request",
        session_id="sess-1",
        cost_usd=0.05,
        tokens_input=100,
        payload={"model_version": "opus-4.8"},
    )
    with EventWriter(settings.store.db_path) as writer:
        writer.write(event)

    conn = connect_read_only(settings.store.db_path)
    try:
        events = fetch_events(conn)
    finally:
        conn.close()

    assert len(events) == 1
    fetched = events[0]
    assert fetched["event_id"] == "evt-live-1"
    assert fetched["source"] == "otlp"
    assert fetched["session_id"] == "sess-1"
    assert fetched["cost_usd"] == 0.05
    assert fetched["tokens_input"] == 100
    assert fetched["payload"] == {"model_version": "opus-4.8"}


def test_fetch_events_missing_payload_decodes_to_empty_dict(settings, event_factory) -> None:
    with EventWriter(settings.store.db_path) as writer:
        writer.write(event_factory(event_id="evt-live-2", payload={}))

    conn = connect_read_only(settings.store.db_path)
    try:
        events = fetch_events(conn)
    finally:
        conn.close()

    assert events[0]["payload"] == {}


def test_run_correction_scan_reads_from_live_events(settings, event_factory, ts) -> None:
    with EventWriter(settings.store.db_path) as writer:
        writer.write_many(
            [
                event_factory(
                    event_id="p1",
                    event_type="user_prompt",
                    session_id="sess-1",
                    ts=ts(0),
                    payload={"text": "add a login form"},
                ),
                event_factory(
                    event_id="p2",
                    event_type="user_prompt",
                    session_id="sess-1",
                    ts=ts(10),
                    payload={"text": "no, that's wrong, undo that"},
                ),
            ]
        )

    conn = connect_read_only(settings.store.db_path)
    try:
        scores = run_correction_scan(conn)
    finally:
        conn.close()

    assert len(scores) == 1
    assert scores[0].session_id == "sess-1"
    assert scores[0].correction_count == 1


def test_run_drift_check_reads_from_live_events(settings, event_factory, ts) -> None:
    with EventWriter(settings.store.db_path) as writer:
        writer.write_many(
            [
                event_factory(event_id="d1", source="otlp", session_id="sess-1", ts=ts(0)),
                event_factory(event_id="d2", source="jsonl", session_id="sess-1", ts=ts(0)),
                event_factory(event_id="d3", source="jsonl", session_id="sess-2", ts=ts(0)),
            ]
        )

    conn = connect_read_only(settings.store.db_path)
    try:
        alerts = run_drift_check(conn, threshold_pct=0.05)
    finally:
        conn.close()

    session_alerts = [a for a in alerts if a.metric == "session_count"]
    assert len(session_alerts) == 1
    assert session_alerts[0].otlp_value == 1
    assert session_alerts[0].jsonl_value == 2


def test_run_error_classification_reads_from_live_events(settings, event_factory) -> None:
    with EventWriter(settings.store.db_path) as writer:
        writer.write(
            event_factory(event_id="e1", event_type="api_error", payload={"error_type": "timeout"})
        )

    conn = connect_read_only(settings.store.db_path)
    try:
        rows = run_error_classification(conn)
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0].category == "timeout"


def test_build_live_callables_sees_a_fresh_snapshot_each_call(settings, event_factory) -> None:
    with EventWriter(settings.store.db_path) as writer:
        writer.write(
            event_factory(event_id="c1", event_type="api_error", payload={"error_type": "timeout"})
        )

    callables = build_live_callables(settings.store.db_path)
    first = callables["error_classification"]()
    assert isinstance(first, list)
    assert len(first) == 1

    with EventWriter(settings.store.db_path) as writer:
        writer.write(
            event_factory(
                event_id="c2", event_type="api_error", payload={"error_type": "rate_limit"}
            )
        )

    second = callables["error_classification"]()
    assert isinstance(second, list)
    assert len(second) == 2


def test_persist_job_status_then_read_job_status_round_trips(settings) -> None:
    result = JobRunResult(
        name="correction_scan",
        status="ok",
        started_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 1, 12, 0, 1, tzinfo=UTC),
    )

    with EventWriter(settings.store.db_path) as writer:
        persist_job_status(writer, result)

    conn = connect_read_only(settings.store.db_path)
    try:
        status = read_job_status(conn, "correction_scan")
    finally:
        conn.close()

    assert status is not None
    assert status["status"] == "ok"
    assert status["error"] is None
    assert status["started_at"] == "2026-07-01T12:00:00+00:00"
    assert status["finished_at"] == "2026-07-01T12:00:01+00:00"


def test_persist_job_status_records_error_details(settings) -> None:
    result = JobRunResult(
        name="drift_check",
        status="error",
        started_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 1, 12, 0, 1, tzinfo=UTC),
        error="boom",
    )

    with EventWriter(settings.store.db_path) as writer:
        persist_job_status(writer, result)

    conn = connect_read_only(settings.store.db_path)
    try:
        status = read_job_status(conn, "drift_check")
    finally:
        conn.close()

    assert status is not None
    assert status["status"] == "error"
    assert status["error"] == "boom"


def test_read_job_status_returns_none_when_job_never_ran(settings) -> None:
    with EventWriter(settings.store.db_path):
        pass  # schema exists; nothing written

    conn = connect_read_only(settings.store.db_path)
    try:
        status = read_job_status(conn, "error_classification")
    finally:
        conn.close()

    assert status is None


def test_persist_job_status_includes_alert_count_for_drift_check(settings) -> None:
    result = JobRunResult(
        name="drift_check",
        status="ok",
        started_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 1, 12, 0, 1, tzinfo=UTC),
        result=[
            DriftAlert(
                day=date(2026, 7, 1),
                metric="session_count",
                otlp_value=1,
                jsonl_value=2,
                drift_pct=0.5,
            )
        ],
    )

    with EventWriter(settings.store.db_path) as writer:
        persist_job_status(writer, result)

    conn = connect_read_only(settings.store.db_path)
    try:
        status = read_job_status(conn, "drift_check")
    finally:
        conn.close()

    assert status is not None
    assert status["name"] == "drift_check"
    assert status["alert_count"] == 1


def test_read_job_status_returns_the_latest_run_per_job_name(settings) -> None:
    first = JobRunResult(
        name="drift_check",
        status="ok",
        started_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 1, 12, 0, 1, tzinfo=UTC),
    )
    second = JobRunResult(
        name="drift_check",
        status="error",
        started_at=datetime(2026, 7, 1, 13, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 1, 13, 0, 1, tzinfo=UTC),
        error="later failure",
    )

    with EventWriter(settings.store.db_path) as writer:
        persist_job_status(writer, first)
        persist_job_status(writer, second)

    conn = connect_read_only(settings.store.db_path)
    try:
        status = read_job_status(conn, "drift_check")
    finally:
        conn.close()

    assert status is not None
    assert status["status"] == "error"
    assert status["error"] == "later failure"
