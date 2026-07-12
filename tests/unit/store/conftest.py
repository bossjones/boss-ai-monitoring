"""Local fixtures for tests/unit/store — owned by 🧱 store, not the shared root conftest.py."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest


@pytest.fixture
def make_event() -> Callable[..., dict[str, Any]]:
    """Factory for a fully-populated canonical Event dict, override any field via kwargs."""

    def _make(event_id: str, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "event_id": event_id,
            "ts": datetime.now(UTC),
            "source": "otlp",
            "event_type": "api_request",
            "session_id": "s1",
            "prompt_id": "p1",
            "request_id": "r1",
            "model": "claude-x",
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
            "success": True,
            "cwd": "/tmp",
            "payload": {},
        }
        base.update(overrides)
        return base

    return _make
