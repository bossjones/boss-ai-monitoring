"""DuckDB schema for the ``events`` / ``ingest_cursors`` tables and the metric views.

``ensure_schema`` is idempotent (``CREATE TABLE IF NOT EXISTS``) so it is safe to call on every
boot. ``load_views`` re-applies ``views.sql`` (``CREATE OR REPLACE VIEW``), so views always
reflect the current source in the repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

_VIEWS_SQL_PATH = Path(__file__).parent / "views.sql"

# Column order shared with store/writer.py — the two must stay in lockstep.
EVENT_COLUMNS = (
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

_CREATE_EVENTS = """
    CREATE TABLE IF NOT EXISTS events (
        event_id VARCHAR PRIMARY KEY,
        ts TIMESTAMP,
        source VARCHAR,
        event_type VARCHAR,
        session_id VARCHAR,
        prompt_id VARCHAR,
        request_id VARCHAR,
        model VARCHAR,
        git_sha VARCHAR,
        agent_name VARCHAR,
        skill_name VARCHAR,
        tool_name VARCHAR,
        cost_usd DOUBLE,
        duration_ms BIGINT,
        tokens_input BIGINT,
        tokens_output BIGINT,
        tokens_cache_read BIGINT,
        tokens_cache_creation BIGINT,
        success BOOLEAN,
        cwd VARCHAR,
        payload JSON
    )
"""

_CREATE_INGEST_CURSORS = """
    CREATE TABLE IF NOT EXISTS ingest_cursors (
        source VARCHAR NOT NULL,
        key VARCHAR NOT NULL,
        cursor VARCHAR,
        updated_at TIMESTAMP,
        PRIMARY KEY (source, key)
    )
"""


def pin_utc(conn: duckdb.DuckDBPyConnection) -> None:
    """Pin the session TimeZone to UTC.

    ``ts``/``updated_at`` are plain TIMESTAMP (not TIMESTAMPTZ): fetching a TIMESTAMPTZ column
    back to Python requires the optional ``pytz`` package, which this project doesn't depend on.
    DuckDB converts an incoming tz-aware Python datetime to the session's local wall clock before
    storing it as a naive TIMESTAMP, so every connection must pin UTC before its first write or
    read or timestamps silently shift by the host's UTC offset.
    """
    conn.execute("SET TimeZone='UTC'")


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create ``events`` and ``ingest_cursors`` if they don't exist yet. Safe on every boot."""
    pin_utc(conn)
    conn.execute(_CREATE_EVENTS)
    conn.execute(_CREATE_INGEST_CURSORS)


def load_views(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply ``store/views.sql`` (``CREATE OR REPLACE VIEW`` for all six metric views)."""
    conn.execute(_VIEWS_SQL_PATH.read_text())
