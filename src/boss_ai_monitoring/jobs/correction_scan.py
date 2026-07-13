"""Correction-language scan (Phase 8, jobs.md).

Heuristic, deterministic per-session correction score (Anthropic's correction-mining analog, G9:
no LLM-judge in v1). Two signals feed the score:

1. A `user_prompt` whose text contains a correction phrase ("no, that's wrong", "undo that", ...).
2. A `user_prompt` that arrives within `reprompt_window_s` of a failed `tool_result` in the same
   session — a re-prompt after a tool failure.

Wave 1 operates on fixture event rows (hermetic); Wave 3 wires this to `connect_read_only()`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from boss_ai_monitoring.jobs._events import Event, ordered_by_ts

DEFAULT_REPROMPT_WINDOW_S = 60.0

CORRECTION_PHRASES: tuple[str, ...] = (
    "no, that's wrong",
    "that's wrong",
    "that's not right",
    "undo that",
    "revert that",
    "not what i asked",
    "try again",
)


@dataclass(frozen=True)
class SessionCorrectionScore:
    """One row per session: how often the user had to correct the agent."""

    session_id: str
    prompt_count: int
    correction_count: int
    phrase_matches: int
    reprompt_matches: int

    @property
    def score(self) -> float:
        return self.correction_count / self.prompt_count if self.prompt_count else 0.0


def _has_correction_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in CORRECTION_PHRASES)


def _prompt_text(payload: object) -> str:
    """The human's words out of a `user_prompt` payload, or "" if there are none.

    The payload is the raw transcript entry (jsonl.py stores it verbatim), so the text lives at
    `message.content` — which is EITHER a plain string or a list of content blocks. This used to
    read `payload["text"]`, a key no ingest source has ever written: 0 of 4522 prompts in the live
    DB have it, so `_has_correction_phrase` was never once reached and the phrase signal was dead
    while the job still reported `ok`.

    The top-level "text" fallback is kept only because some tests still build payloads that way.
    """
    if not isinstance(payload, dict):
        return ""

    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # content blocks: text lives in the "text" blocks; tool_result/image blocks have none
        return " ".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )

    fallback = payload.get("text")
    return fallback if isinstance(fallback, str) else ""


def scan_corrections(
    events: Iterable[Event],
    *,
    reprompt_window_s: float = DEFAULT_REPROMPT_WINDOW_S,
) -> list[SessionCorrectionScore]:
    """Per-session correction score over the given events, sorted by session_id.

    Sessions with no `user_prompt` events are omitted; sessions with prompts but no correction
    signal still appear with `correction_count == 0`.
    """
    by_session: dict[str, list[Event]] = {}
    for event in events:
        session_id = event.get("session_id")
        if session_id is None:
            continue
        by_session.setdefault(session_id, []).append(event)

    scores: list[SessionCorrectionScore] = []
    for session_id, session_events in by_session.items():
        # drops ts-less rows: they can be neither ordered nor differenced (see ordered_by_ts)
        ordered = ordered_by_ts(session_events)

        prompt_count = 0
        correction_count = 0
        phrase_matches = 0
        reprompt_matches = 0
        last_failed_tool_ts = None

        for event in ordered:
            event_type = event.get("event_type")

            if event_type == "tool_result":
                last_failed_tool_ts = (
                    event["ts"] if event.get("success") is False else last_failed_tool_ts
                )
                continue

            if event_type != "user_prompt":
                continue

            prompt_count += 1
            text = _prompt_text(event.get("payload"))
            is_phrase_match = bool(text) and _has_correction_phrase(text)
            is_reprompt_match = last_failed_tool_ts is not None and (
                0 <= (event["ts"] - last_failed_tool_ts).total_seconds() <= reprompt_window_s
            )

            phrase_matches += is_phrase_match
            reprompt_matches += is_reprompt_match
            if is_phrase_match or is_reprompt_match:
                correction_count += 1

        if prompt_count == 0:
            continue

        scores.append(
            SessionCorrectionScore(
                session_id=session_id,
                prompt_count=prompt_count,
                correction_count=correction_count,
                phrase_matches=phrase_matches,
                reprompt_matches=reprompt_matches,
            )
        )

    return sorted(scores, key=lambda s: s.session_id)
