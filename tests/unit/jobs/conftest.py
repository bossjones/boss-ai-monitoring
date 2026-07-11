"""Fixture-row helpers for the jobs test suite (jobs-owned, distinct from the root conftest.py).

Wave 1 is hermetic: every job under test here operates on plain fixture Event dicts, never a live
DuckDB connection. The shape mirrors the canonical envelope published in
.team/boss-ai-monitoring-build.backlog.md (BL-01) — jobs does not import it from store, since
store hasn't published a concrete module yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest

_event_id_counter = count(1)


def make_event(**overrides: Any) -> dict[str, Any]:
    """Build one Event dict with sane defaults; override only what a test cares about."""
    defaults: dict[str, Any] = {
        "event_id": f"evt-{next(_event_id_counter)}",
        "ts": datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        "source": "otlp",
        "event_type": "api_request",
        "session_id": "sess-1",
        "prompt_id": None,
        "request_id": None,
        "model": None,
        "git_sha": None,
        "agent_name": None,
        "skill_name": None,
        "tool_name": None,
        "cost_usd": None,
        "duration_ms": None,
        "tokens_input": None,
        "tokens_output": None,
        "tokens_cache_read": None,
        "tokens_cache_creation": None,
        "success": None,
        "cwd": None,
        "payload": {},
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def event_factory() -> Any:
    """Per-test counter reset isn't needed — event_id just needs to be unique within a run."""
    return make_event


@pytest.fixture
def ts() -> Any:
    """A base timestamp plus a `+seconds` helper, so tests can express relative timing plainly."""
    base = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

    def _at(seconds: float = 0) -> datetime:
        return base + timedelta(seconds=seconds)

    return _at
