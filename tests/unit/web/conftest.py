"""Hermetic web-pane fixtures: an in-memory DuckDB connection seeded through store's real schema.

Wave 3: LT-01 lifted the Wave 1 restriction on importing ``boss_ai_monitoring.store`` (it is GREEN
and committed). ``fixture_conn`` uses store's own ``ensure_schema``/``load_views`` so tests read
through the SAME six views production code does, rather than a hand-rolled table shape -- still a
fresh in-memory connection per test, never a real file on disk.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import duckdb
import pytest

from boss_ai_monitoring.store.schema import ensure_schema, load_views

InsertEvent = Callable[..., None]

_STATIC_DEFAULT_ROW: dict[str, Any] = {
    "event_id": "evt-1",
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
    """An in-memory DuckDB connection with store's real schema + views, empty of data."""
    conn = duckdb.connect(":memory:")
    ensure_schema(conn)
    load_views(conn)
    yield conn
    conn.close()


@pytest.fixture
def insert_event() -> InsertEvent:
    """Factory: ``insert_event(conn, **overrides)`` inserts one fixture row with MVP defaults."""

    def _insert(conn: duckdb.DuckDBPyConnection, **overrides: Any) -> None:
        # `ts` defaults to "now" (not a fixed literal) so tests that rely on it matching "today"
        # in route handlers (real `datetime.now(UTC)`, not a test-injected `now`) stay correct
        # across a real calendar-day boundary.
        row = dict(_STATIC_DEFAULT_ROW)
        row["ts"] = datetime.now(UTC)
        row.update(overrides)
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO events ({columns}) VALUES ({placeholders})", list(row.values()))

    return _insert
