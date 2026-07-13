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


# --- real jsonl payload shapes -------------------------------------------------------------
#
# The fixtures above use `payload={"text": ...}`, which NO ingest source has ever produced. Against
# the live DB, 0 of 4522 `user_prompt` events have a top-level "text" key — the prompt text lives at
# `payload.message.content`, either a plain string (3787x) or a content-block array (735x). So the
# phrase matcher never fired once in production while these tests stayed green. These pin the shapes
# that actually exist.


def test_correction_phrase_is_detected_in_a_real_jsonl_string_payload(event_factory, ts) -> None:
    events = [
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(0),
            payload={"type": "user", "message": {"role": "user", "content": "add a login form"}},
        ),
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(30),
            payload={
                "type": "user",
                "message": {"role": "user", "content": "no, that's wrong — undo that"},
            },
        ),
    ]

    scores = scan_corrections(events)

    assert scores[0].prompt_count == 2
    assert scores[0].phrase_matches == 1


def test_correction_phrase_is_detected_in_a_real_jsonl_content_block_payload(
    event_factory, ts
) -> None:
    events = [
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(0),
            payload={
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "that's not right, "},
                        {"type": "text", "text": "please try again"},
                    ],
                },
            },
        )
    ]

    scores = scan_corrections(events)

    assert scores[0].prompt_count == 1
    assert scores[0].phrase_matches == 1


def test_events_with_no_timestamp_are_dropped_not_crashed(event_factory, ts) -> None:
    """`jsonl._parse_ts` returns None for a malformed `timestamp`, and `live.fetch_events` hands
    those rows straight to this job — so `sorted(key=e["ts"])` raised
    `TypeError: '<' not supported between instances of 'NoneType' and 'datetime.datetime'`,
    which the scheduler swallowed into a permanent `correction_scan: error` badge.

    A ts-less event cannot be ordered OR differenced, so it cannot sit on a timeline at all: drop
    it, exactly as `drift_check` already does. The timestamped prompts still score.
    """
    events = [
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=ts(0),
            payload={"message": {"content": "no, that's wrong"}},
        ),
        event_factory(
            event_type="user_prompt",
            session_id="sess-1",
            ts=None,  # malformed transcript timestamp
            payload={"message": {"content": "undo that"}},
        ),
        event_factory(
            event_type="tool_result", session_id="sess-1", ts=None, success=False, payload={}
        ),
    ]

    scores = scan_corrections(events)  # must not raise

    assert len(scores) == 1
    assert scores[0].prompt_count == 1, "only the timestamped prompt can be scored"
    assert scores[0].phrase_matches == 1
