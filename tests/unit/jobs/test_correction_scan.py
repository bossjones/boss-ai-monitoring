"""RED-FIRST: correction-language scan (Phase 8, jobs.md).

Heuristic per-session correction score over fixture user_prompt / tool_result events. No live DB —
Wave 1 is hermetic (BL-01 stub only, no store import).
"""

from __future__ import annotations

from boss_ai_monitoring.jobs.correction_scan import scan_corrections


def test_empty_events_returns_no_scores() -> None:
    assert scan_corrections([]) == []


def test_session_with_no_correction_signals_scores_zero(event_factory, ts) -> None:
    events = [
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(0),
            payload={"text": "add a login form"},
        ),
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(30),
            payload={"text": "now add tests"},
        ),
    ]

    scores = scan_corrections(events)

    assert len(scores) == 1
    assert scores[0].session_id == "sess-1"
    assert scores[0].prompt_count == 2
    assert scores[0].correction_count == 0
    assert scores[0].score == 0.0


def test_correction_phrase_in_user_prompt_is_detected(event_factory, ts) -> None:
    events = [
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(0),
            payload={"text": "add a login form"},
        ),
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(30),
            payload={"text": "no, that's wrong, revert that"},
        ),
    ]

    scores = scan_corrections(events)

    assert scores[0].prompt_count == 2
    assert scores[0].correction_count == 1
    assert scores[0].phrase_matches == 1
    assert scores[0].reprompt_matches == 0
    assert scores[0].score == 0.5


def test_reprompt_within_60s_of_failed_tool_result_is_detected(event_factory, ts) -> None:
    events = [
        event_factory(
            event_type="tool_result",
            session_id="sess-1",
            ts=ts(0),
            tool_name="bash",
            success=False,
        ),
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(45),
            payload={"text": "try a different approach"},
        ),
    ]

    scores = scan_corrections(events)

    assert scores[0].prompt_count == 1
    assert scores[0].correction_count == 1
    assert scores[0].phrase_matches == 0
    assert scores[0].reprompt_matches == 1


def test_reprompt_outside_60s_window_is_not_detected(event_factory, ts) -> None:
    events = [
        event_factory(
            event_type="tool_result",
            session_id="sess-1",
            ts=ts(0),
            tool_name="bash",
            success=False,
        ),
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(61),
            payload={"text": "try a different approach"},
        ),
    ]

    scores = scan_corrections(events)

    assert scores[0].correction_count == 0
    assert scores[0].reprompt_matches == 0


def test_successful_tool_result_does_not_trigger_reprompt_signal(event_factory, ts) -> None:
    events = [
        event_factory(
            event_type="tool_result",
            session_id="sess-1",
            ts=ts(0),
            tool_name="bash",
            success=True,
        ),
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(10),
            payload={"text": "now add tests"},
        ),
    ]

    scores = scan_corrections(events)

    assert scores[0].correction_count == 0
    assert scores[0].reprompt_matches == 0


def test_both_signals_on_one_prompt_count_once_towards_correction_count(event_factory, ts) -> None:
    events = [
        event_factory(
            event_type="tool_result",
            session_id="sess-1",
            ts=ts(0),
            tool_name="bash",
            success=False,
        ),
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(10),
            payload={"text": "no, that's wrong, undo that"},
        ),
    ]

    scores = scan_corrections(events)

    assert scores[0].prompt_count == 1
    assert scores[0].correction_count == 1
    assert scores[0].phrase_matches == 1
    assert scores[0].reprompt_matches == 1


def test_scores_are_grouped_and_sorted_per_session(event_factory, ts) -> None:
    events = [
        event_factory(
            event_type="user_prompt", session_id="sess-b", ts=ts(0), payload={"text": "hi"}
        ),
        event_factory(
            event_type="user_prompt", session_id="sess-a", ts=ts(0), payload={"text": "hi"}
        ),
        event_factory(
            event_type="user_prompt",
            session_id="sess-a",
            ts=ts(5),
            payload={"text": "no, that's wrong"},
        ),
    ]

    scores = scan_corrections(events)

    assert [s.session_id for s in scores] == ["sess-a", "sess-b"]
    assert scores[0].correction_count == 1
    assert scores[1].correction_count == 0


def test_events_without_session_id_are_ignored(event_factory, ts) -> None:
    events = [
        event_factory(
            event_type="user_prompt",
            session_id=None,
            ts=ts(0),
            payload={"text": "no, that's wrong"},
        ),
    ]

    assert scan_corrections(events) == []
