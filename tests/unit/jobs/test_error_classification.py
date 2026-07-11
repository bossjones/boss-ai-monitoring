"""RED-FIRST: error classification rollup (Phase 8, jobs.md).

Groups `api_error` / failed `tool_result.error_type` into a taxonomy table for the dashboard panel
(Sniffly's most-loved feature). Deterministic bucket matching only — no LLM-judge (G9). Hermetic
fixture rows only (Wave 1) — no live DB.
"""

from __future__ import annotations

from boss_ai_monitoring.jobs.error_classification import classify_error_type, classify_errors


def test_empty_events_produces_no_rows() -> None:
    assert classify_errors([]) == []


def test_successful_tool_result_is_ignored(event_factory) -> None:
    events = [
        event_factory(event_type="tool_result", success=True, payload={"error_type": "timeout"}),
    ]

    assert classify_errors(events) == []


def test_unrelated_event_types_are_ignored(event_factory) -> None:
    events = [event_factory(event_type="api_request")]

    assert classify_errors(events) == []


def test_failed_tool_result_is_classified(event_factory) -> None:
    events = [
        event_factory(
            event_type="tool_result",
            success=False,
            payload={"error_type": "timeout"},
        ),
    ]

    rows = classify_errors(events)

    assert len(rows) == 1
    assert rows[0].category == "timeout"
    assert rows[0].error_type == "timeout"
    assert rows[0].event_type == "tool_result"
    assert rows[0].count == 1


def test_api_error_is_classified(event_factory) -> None:
    events = [
        event_factory(event_type="api_error", payload={"error_type": "rate_limit_error"}),
    ]

    rows = classify_errors(events)

    assert rows[0].category == "rate_limit"
    assert rows[0].event_type == "api_error"


def test_missing_error_type_buckets_as_unknown(event_factory) -> None:
    events = [event_factory(event_type="api_error", payload={})]

    rows = classify_errors(events)

    assert rows[0].category == "unknown"
    assert rows[0].error_type is None


def test_unrecognized_error_type_buckets_as_other(event_factory) -> None:
    events = [event_factory(event_type="api_error", payload={"error_type": "teapot_exploded"})]

    rows = classify_errors(events)

    assert rows[0].category == "other"


def test_same_category_and_type_are_counted_together(event_factory) -> None:
    events = [
        event_factory(event_type="api_error", payload={"error_type": "timeout"}),
        event_factory(event_type="api_error", payload={"error_type": "timeout"}),
        event_factory(event_type="api_error", payload={"error_type": "deadline_exceeded"}),
    ]

    rows = classify_errors(events)

    assert len(rows) == 2
    timeout_row = next(r for r in rows if r.error_type == "timeout")
    assert timeout_row.count == 2
    deadline_row = next(r for r in rows if r.error_type == "deadline_exceeded")
    assert deadline_row.count == 1
    assert deadline_row.category == "timeout"


def test_rows_are_sorted_by_count_descending(event_factory) -> None:
    events = [
        event_factory(event_type="api_error", payload={"error_type": "auth_error"}),
        event_factory(event_type="api_error", payload={"error_type": "rate_limit"}),
        event_factory(event_type="api_error", payload={"error_type": "rate_limit"}),
    ]

    rows = classify_errors(events)

    assert [r.error_type for r in rows] == ["rate_limit", "auth_error"]


def test_classify_error_type_buckets_directly() -> None:
    assert classify_error_type("429 Too Many Requests") == "rate_limit"
    assert classify_error_type("Connection reset by peer") == "network"
    assert classify_error_type("Unauthorized") == "auth"
    assert classify_error_type("Invalid request body") == "validation"
    assert classify_error_type(None) == "unknown"
    assert classify_error_type("") == "unknown"
    assert classify_error_type("something_bespoke") == "other"
