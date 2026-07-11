"""RED-first tests for store/schema.py: table shape for events + ingest_cursors."""

from __future__ import annotations

from typing import Any

import duckdb
import pytest

from boss_ai_monitoring.store.schema import EVENT_COLUMNS, ensure_schema, load_views

EXPECTED_EVENT_COLUMNS = (
    "event_id",
    "ts",
    "source",
    "event_type",
    "session_id",
    "prompt_id",
    "request_id",
    "model",
    "git_sha",
    "agent_name",
    "skill_name",
    "tool_name",
    "cost_usd",
    "duration_ms",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_creation",
    "success",
    "cwd",
    "payload",
)


@pytest.fixture
def conn() -> Any:
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def _column_names(connection: Any, table: str) -> list[str]:
    rows = connection.execute(f"PRAGMA table_info('{table}')").fetchall()
    return [row[1] for row in rows]


def test_event_columns_constant_matches_spec() -> None:
    assert EVENT_COLUMNS == EXPECTED_EVENT_COLUMNS


def test_ensure_schema_creates_events_table_with_exact_columns(conn: Any) -> None:
    ensure_schema(conn)

    assert _column_names(conn, "events") == list(EXPECTED_EVENT_COLUMNS)


def test_ensure_schema_creates_ingest_cursors_table(conn: Any) -> None:
    ensure_schema(conn)

    assert _column_names(conn, "ingest_cursors") == ["source", "key", "cursor", "updated_at"]


def test_ensure_schema_is_idempotent(conn: Any) -> None:
    ensure_schema(conn)
    ensure_schema(conn)  # must not raise

    assert _column_names(conn, "events") == list(EXPECTED_EVENT_COLUMNS)


def test_events_table_rejects_duplicate_event_id(conn: Any) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO events (event_id, ts, source, event_type) VALUES (?, now(), ?, ?)",
        ["e1", "otlp", "api_request"],
    )

    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO events (event_id, ts, source, event_type) VALUES (?, now(), ?, ?)",
            ["e1", "otlp", "api_request"],
        )


def test_load_views_creates_all_six_views(conn: Any) -> None:
    ensure_schema(conn)
    load_views(conn)

    views = {
        row[0]
        for row in conn.execute(
            "SELECT view_name FROM duckdb_views() WHERE schema_name = 'main'"
        ).fetchall()
    }
    assert {
        "v_sessions",
        "v_tasks",
        "v_costs_daily",
        "v_tool_stats",
        "v_attribution",
        "v_five_metrics",
    } <= views


def test_load_views_is_idempotent(conn: Any) -> None:
    ensure_schema(conn)
    load_views(conn)
    load_views(conn)  # CREATE OR REPLACE must not raise
