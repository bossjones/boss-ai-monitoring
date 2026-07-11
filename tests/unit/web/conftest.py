"""Hermetic web-pane fixtures: an in-memory DuckDB connection seeded by hand.

Wave 1 SHADOW: web does not import ``boss_ai_monitoring.store`` (it is not GREEN yet). This module
defines the minimal ``events`` table shape needed to exercise ``web/queries.py`` against fixture
rows, matching the canonical event envelope published in
``specs/boss-ai-monitoring/briefs/shared.md`` (BL-01). Wave 3 wires web to the real store; this
schema is a local stand-in, not a claim about store's actual DDL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import duckdb
import pytest

InsertEvent = Callable[..., None]

EVENTS_SCHEMA = """
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    ts TIMESTAMP,
    source TEXT,
    event_type TEXT,
    session_id TEXT,
    prompt_id TEXT,
    request_id TEXT,
    model TEXT,
    git_sha TEXT,
    agent_name TEXT,
    skill_name TEXT,
    tool_name TEXT,
    cost_usd DOUBLE,
    duration_ms INTEGER,
    tokens_input INTEGER,
    tokens_output INTEGER,
    tokens_cache_read INTEGER,
    tokens_cache_creation INTEGER,
    success BOOLEAN,
    cwd TEXT,
    payload JSON
)
"""

_DEFAULT_ROW: dict[str, Any] = {
    "event_id": "evt-1",
    "ts": datetime(2026, 7, 11, 12, 0, 0),
    "source": "otlp",
    "event_type": "api_request",
    "session_id": "sess-1",
    "prompt_id": "prompt-1",
    "request_id": "req-1",
    "model": "claude-sonnet-5",
    "git_sha": None,
    "agent_name": None,
    "skill_name": None,
    "tool_name": None,
    "cost_usd": 0.01,
    "duration_ms": 100,
    "tokens_input": 10,
    "tokens_output": 5,
    "tokens_cache_read": 0,
    "tokens_cache_creation": 0,
    "success": None,
    "cwd": None,
    "payload": "{}",
}


@pytest.fixture
def fixture_conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """An in-memory DuckDB connection with an empty ``events`` table."""
    conn = duckdb.connect(":memory:")
    conn.execute(EVENTS_SCHEMA)
    yield conn
    conn.close()


@pytest.fixture
def insert_event() -> InsertEvent:
    """Factory: ``insert_event(conn, **overrides)`` inserts one fixture row with MVP defaults."""

    def _insert(conn: duckdb.DuckDBPyConnection, **overrides: Any) -> None:
        row = dict(_DEFAULT_ROW)
        row.update(overrides)
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO events ({columns}) VALUES ({placeholders})", list(row.values()))

    return _insert
