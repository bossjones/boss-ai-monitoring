"""POST /api/snapshot — the running app hands out a consistent copy of its own DB.

The app owns the only write connection, so it is the only process that CAN do this while it is
serving (OQ-05). The endpoint deliberately does NOT accept a destination path from the caller: an
HTTP endpoint that writes to an arbitrary caller-chosen path is a file-write primitive, and
"it only binds localhost" (G10) is not a good enough reason to build one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI
from fastapi.testclient import TestClient

from boss_ai_monitoring.config import BamSettings
from boss_ai_monitoring.store.writer import get_writer
from boss_ai_monitoring.web.app import create_app


def _event(event_id: str) -> dict[str, Any]:
    """A minimal canonical event. (store's `make_event` fixture is scoped to tests/unit/store.)"""
    return {
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


def test_snapshot_endpoint_returns_a_readable_copy_while_serving(
    settings: BamSettings,
    client_factory: Callable[[FastAPI], TestClient],
) -> None:
    writer = get_writer(settings)
    try:
        writer.write_many([_event("a")])
        writer.flush()

        client = client_factory(create_app(settings))
        response = client.post("/api/snapshot")

        assert response.status_code == 200
        body = response.json()
        dest = Path(body["path"])
        assert dest.exists()
        assert body["rows"] == 1

        # readable from a wholly separate connection, while the writer above is still live
        conn = duckdb.connect(str(dest), read_only=True)
        try:
            assert conn.execute("SELECT count(*) FROM events").fetchone() == (1,)
        finally:
            conn.close()
    finally:
        writer.close()


def test_snapshot_endpoint_lands_in_the_configured_dir(
    settings: BamSettings,
    client_factory: Callable[[FastAPI], TestClient],
) -> None:
    client = client_factory(create_app(settings))
    body = client.post("/api/snapshot").json()
    assert Path(body["path"]).parent == settings.store.snapshot_dir


def test_snapshot_endpoint_ignores_a_caller_supplied_path(
    settings: BamSettings,
    client_factory: Callable[[FastAPI], TestClient],
    tmp_path: Path,
) -> None:
    """A caller must not be able to steer the write. The server picks the path, always."""
    evil = tmp_path / "pwned.duckdb"
    client = client_factory(create_app(settings))

    body = client.post("/api/snapshot", json={"path": str(evil)}).json()

    assert not evil.exists()
    assert Path(body["path"]).parent == settings.store.snapshot_dir


def test_repeated_snapshots_do_not_collide(
    settings: BamSettings,
    client_factory: Callable[[FastAPI], TestClient],
) -> None:
    """snapshot_to() refuses to clobber, so two calls in a row must not pick the same name."""
    client = client_factory(create_app(settings))
    first = client.post("/api/snapshot")
    second = client.post("/api/snapshot")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["path"] != second.json()["path"]
