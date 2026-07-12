"""OTel-vs-JSONL drift self-check (Phase 8, jobs.md).

The standing mitigation for spec RISK #1 (JSONL format drift across Claude Code versions):
compare OTel-derived vs JSONL-derived session/token totals per day, and flag days where the two
lineages disagree beyond a threshold. This is the "silent failure" sanity-check pattern — the
alert row is the whole point, not the raw totals.

Wave 1 operates on fixture event rows (hermetic); Wave 3 wires this to `connect_read_only()`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from boss_ai_monitoring.jobs._events import Event

DEFAULT_THRESHOLD_PCT = 0.05

_COMPARED_SOURCES = ("otlp", "jsonl")


@dataclass(frozen=True)
class DriftAlert:
    """One row: a single metric, on a single day, where the two lineages disagree."""

    day: date
    metric: str  # "session_count" | "token_total"
    otlp_value: float
    jsonl_value: float
    drift_pct: float


def _token_total(event: Event) -> int:
    return (event.get("tokens_input") or 0) + (event.get("tokens_output") or 0)


def _drift_pct(otlp_value: float, jsonl_value: float) -> float:
    largest = max(otlp_value, jsonl_value)
    if largest == 0:
        return 0.0
    return abs(otlp_value - jsonl_value) / largest


def check_drift(
    events: Iterable[Event],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
) -> list[DriftAlert]:
    """Alert rows for days where OTel- and JSONL-derived totals drift beyond `threshold_pct`."""
    session_ids: dict[date, dict[str, set[str]]] = {}
    token_totals: dict[date, dict[str, int]] = {}

    for event in events:
        source = event.get("source")
        if source not in _COMPARED_SOURCES:
            continue
        ts = event.get("ts")
        if ts is None:
            continue
        day = ts.date()

        session_id = event.get("session_id")
        if session_id is not None:
            session_ids.setdefault(day, {"otlp": set(), "jsonl": set()})[source].add(session_id)

        token_totals.setdefault(day, {"otlp": 0, "jsonl": 0})
        token_totals[day][source] += _token_total(event)

    days = sorted(set(session_ids) | set(token_totals))
    alerts: list[DriftAlert] = []

    for day in days:
        day_sessions = session_ids.get(day, {"otlp": set(), "jsonl": set()})
        otlp_sessions, jsonl_sessions = len(day_sessions["otlp"]), len(day_sessions["jsonl"])
        drift = _drift_pct(otlp_sessions, jsonl_sessions)
        if drift > threshold_pct:
            alerts.append(
                DriftAlert(
                    day=day,
                    metric="session_count",
                    otlp_value=otlp_sessions,
                    jsonl_value=jsonl_sessions,
                    drift_pct=drift,
                )
            )

        day_tokens = token_totals.get(day, {"otlp": 0, "jsonl": 0})
        otlp_tokens, jsonl_tokens = day_tokens["otlp"], day_tokens["jsonl"]
        drift = _drift_pct(otlp_tokens, jsonl_tokens)
        if drift > threshold_pct:
            alerts.append(
                DriftAlert(
                    day=day,
                    metric="token_total",
                    otlp_value=otlp_tokens,
                    jsonl_value=jsonl_tokens,
                    drift_pct=drift,
                )
            )

    return alerts
