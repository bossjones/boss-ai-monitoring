"""RED-FIRST: OTel-vs-JSONL drift self-check (Phase 8, jobs.md).

Standing mitigation for spec RISK #1 (JSONL format drift). Compares OTel-derived vs JSONL-derived
session/token totals per day; drift beyond threshold produces an alert row. Hermetic fixture rows
only (Wave 1) — no live DB.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from boss_ai_monitoring.jobs.drift_check import check_drift


def _day(offset_days: int = 0) -> datetime:
    return datetime(2026, 7, 1 + offset_days, 12, 0, 0, tzinfo=UTC)


def test_empty_events_produces_no_alerts() -> None:
    assert check_drift([]) == []


def test_all_sources_agree_produces_no_alerts(event_factory) -> None:
    events = [
        event_factory(
            source="otlp",
            event_type="api_request",
            session_id="sess-1",
            ts=_day(0),
            tokens_input=100,
            tokens_output=50,
        ),
        event_factory(
            source="jsonl",
            event_type="api_request",
            session_id="sess-1",
            ts=_day(0),
            tokens_input=100,
            tokens_output=50,
        ),
    ]

    assert check_drift(events) == []


def test_session_count_drift_beyond_threshold_produces_alert(event_factory) -> None:
    events = [
        event_factory(source="otlp", session_id="sess-1", ts=_day(0)),
        event_factory(source="jsonl", session_id="sess-1", ts=_day(0)),
        event_factory(source="jsonl", session_id="sess-2", ts=_day(0)),
    ]

    alerts = check_drift(events, threshold_pct=0.05)

    session_alerts = [a for a in alerts if a.metric == "session_count"]
    assert len(session_alerts) == 1
    alert = session_alerts[0]
    assert alert.day == date(2026, 7, 1)
    assert alert.otlp_value == 1
    assert alert.jsonl_value == 2
    assert alert.drift_pct == 0.5


def test_token_total_drift_beyond_threshold_produces_alert(event_factory) -> None:
    events = [
        event_factory(
            source="otlp", session_id="sess-1", ts=_day(0), tokens_input=100, tokens_output=0
        ),
        event_factory(
            source="jsonl", session_id="sess-1", ts=_day(0), tokens_input=80, tokens_output=0
        ),
    ]

    alerts = check_drift(events, threshold_pct=0.05)

    token_alerts = [a for a in alerts if a.metric == "token_total"]
    assert len(token_alerts) == 1
    alert = token_alerts[0]
    assert alert.otlp_value == 100
    assert alert.jsonl_value == 80
    assert alert.drift_pct == 0.2


def test_drift_within_threshold_produces_no_alert(event_factory) -> None:
    events = [
        event_factory(
            source="otlp", session_id="sess-1", ts=_day(0), tokens_input=100, tokens_output=0
        ),
        event_factory(
            source="jsonl", session_id="sess-1", ts=_day(0), tokens_input=99, tokens_output=0
        ),
    ]

    assert check_drift(events, threshold_pct=0.05) == []


def test_missing_jsonl_data_for_a_day_is_full_drift(event_factory) -> None:
    events = [
        event_factory(
            source="otlp", session_id="sess-1", ts=_day(0), tokens_input=100, tokens_output=0
        ),
    ]

    alerts = check_drift(events, threshold_pct=0.05)

    token_alerts = [a for a in alerts if a.metric == "token_total"]
    assert token_alerts[0].jsonl_value == 0
    assert token_alerts[0].drift_pct == 1.0


def test_alerts_are_scoped_per_day(event_factory) -> None:
    events = [
        event_factory(source="otlp", session_id="sess-1", ts=_day(0)),
        event_factory(source="jsonl", session_id="sess-1", ts=_day(0)),
        event_factory(source="otlp", session_id="sess-2", ts=_day(1)),
        event_factory(source="jsonl", session_id="sess-2", ts=_day(1)),
        event_factory(source="jsonl", session_id="sess-3", ts=_day(1)),
    ]

    alerts = check_drift(events, threshold_pct=0.05)

    assert len(alerts) == 1
    assert alerts[0].day == date(2026, 7, 2)


def test_langsmith_sourced_events_are_ignored(event_factory) -> None:
    events = [
        event_factory(source="otlp", session_id="sess-1", ts=_day(0)),
        event_factory(source="jsonl", session_id="sess-1", ts=_day(0)),
        event_factory(source="langsmith", session_id="sess-99", ts=_day(0)),
    ]

    assert check_drift(events, threshold_pct=0.05) == []
