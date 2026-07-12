"""Shared fixtures for the whole suite.

Owned by the lead; every pane consumes these rather than rolling its own tmp-DB or client
factory. Add new shared fixtures via a backlog item, not by editing this file from another pane.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

BAM_ENV_PREFIX = "BAM_"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient BAM_* config so a developer's shell can't change a test outcome."""
    for key in list(os.environ):
        if key.startswith(BAM_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A tmp DuckDB file path. The file is not created — the writer owns creation."""
    return tmp_path / "bam.duckdb"


@pytest.fixture
def settings(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """BamSettings pointed at a tmp DuckDB file and an empty projects dir."""
    from boss_ai_monitoring.config import BamSettings

    monkeypatch.setenv("BAM_STORE__DB_PATH", str(db_path))
    return BamSettings()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the VCR record-mode option consumed by tests/integration/langsmith."""
    parser.addoption(
        "--vcr-mode",
        type=str,
        default="none",
        help=(
            "VCR record mode: none = replay-only (default, hermetic), "
            "once = record if cassette missing, all = re-record against the live API"
        ),
    )


@pytest.fixture
def client_factory() -> Iterator[Callable[[FastAPI], TestClient]]:
    """Factory turning a FastAPI app into a TestClient, closed at teardown."""
    from fastapi.testclient import TestClient

    clients: list[TestClient] = []

    def _make(app: FastAPI) -> TestClient:
        client = TestClient(app)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.close()
