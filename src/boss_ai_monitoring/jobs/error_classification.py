"""Error classification rollup (Phase 8, jobs.md).

Groups `api_error` events and failed `tool_result` events into a deterministic taxonomy
(Sniffly's most-loved feature, per shared.md). No LLM-judge in v1 (G9) — bucket matching is a
plain substring lookup against `payload["error_type"]`.

Wave 1 operates on fixture event rows (hermetic); Wave 3 wires this to `connect_read_only()`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from boss_ai_monitoring.jobs._events import Event

_CLASSIFIED_EVENT_TYPES = ("api_error", "tool_result")

TAXONOMY: dict[str, tuple[str, ...]] = {
    "rate_limit": ("rate_limit", "429", "quota", "too_many_requests"),
    "timeout": ("timeout", "deadline_exceeded"),
    "auth": ("auth", "unauthorized", "403", "401", "permission_denied", "forbidden"),
    "network": ("network", "connection", "econnreset", "dns", "unreachable"),
    "validation": ("validation", "invalid", "bad_request", "400"),
}


@dataclass(frozen=True)
class ErrorClassificationRow:
    """One row per (category, event_type, error_type) with its occurrence count."""

    category: str
    error_type: str | None
    event_type: str
    count: int


def classify_error_type(error_type: str | None) -> str:
    """Deterministic bucket for a raw error_type string. `None`/empty -> "unknown"."""
    if not error_type:
        return "unknown"
    lowered = error_type.lower()
    for category, needles in TAXONOMY.items():
        if any(needle in lowered for needle in needles):
            return category
    return "other"


def _error_type_of(event: Event) -> str | None:
    return event.get("payload", {}).get("error_type")


def classify_errors(events: Iterable[Event]) -> list[ErrorClassificationRow]:
    """Rollup of `api_error` and failed `tool_result` events, sorted by count descending."""
    counts: dict[tuple[str, str, str | None], int] = {}

    for event in events:
        event_type = event.get("event_type")
        if event_type == "api_error" or (
            event_type == "tool_result" and event.get("success") is False
        ):
            error_type = _error_type_of(event)
        else:
            continue

        category = classify_error_type(error_type)
        key = (category, event_type, error_type)
        counts[key] = counts.get(key, 0) + 1

    rows = [
        ErrorClassificationRow(
            category=category, event_type=event_type, error_type=error_type, count=count
        )
        for (category, event_type, error_type), count in counts.items()
    ]
    return sorted(rows, key=lambda r: (-r.count, r.category, r.event_type, r.error_type or ""))
