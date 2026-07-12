"""Local fixtures for tests/unit/ingest — owned by 📜 jsonl, not the shared root conftest.py."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "jsonl"

# G14: LANGSMITH_*/LANGCHAIN_* arrive ambiently via direnv in every real shell — a poller test
# must never see or depend on them, or "missing API key" tests silently pass against a real key.
_LANGSMITH_ENV_PREFIXES = ("LANGSMITH_", "LANGCHAIN_")

LANGSMITH_API_URL = "http://testserver"


@pytest.fixture(autouse=True)
def isolated_langsmith_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(_LANGSMITH_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    """A tmp `~/.claude/projects`-shaped directory: one subdir per project, *.jsonl inside."""
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def copy_fixture() -> Callable[..., Path]:
    """Factory: copy a fixture transcript into a project subdir, returns its path."""

    def _copy(name: str, dest_dir: Path, filename: str | None = None) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (filename or name)
        shutil.copyfile(FIXTURES_DIR / name, dest)
        return dest

    return _copy


@pytest.fixture
def mock_langsmith_project() -> Callable[..., str]:
    """Factory: register GET /sessions?name=... so `read_project` resolves to a project_id."""

    def _mock(respx_mock: Any, *, project_name: str, project_id: str | None = None) -> str:
        project_id = project_id or str(uuid.uuid4())
        respx_mock.get(f"{LANGSMITH_API_URL}/sessions", params={"name": project_name}).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": project_id,
                        "tenant_id": str(uuid.uuid4()),
                        "reference_dataset_id": None,
                        "name": project_name,
                    }
                ],
            )
        )
        return project_id

    return _mock


@pytest.fixture
def make_langsmith_run() -> Callable[..., dict[str, Any]]:
    """Factory: one LangSmith Run dict, shaped as the API would serialize it."""

    def _make(
        *,
        run_id: str | None = None,
        trace_id: str | None = None,
        start_time: datetime,
        end_time: datetime | None = None,
        run_type: str = "llm",
        name: str = "ChatAnthropic",
        thread_id: str | None = None,
        prompt_tokens: int | None = 10,
        completion_tokens: int | None = 5,
        total_tokens: int | None = 15,
        total_cost: str | None = "0.01",
        status: str = "success",
        error: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or str(uuid.uuid4())
        metadata: dict[str, Any] = {}
        if thread_id is not None:
            metadata["thread_id"] = thread_id
        return {
            "id": run_id,
            "trace_id": trace_id or run_id,
            "name": name,
            "run_type": run_type,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat() if end_time else None,
            "status": status,
            "error": error,
            "extra": {"metadata": metadata},
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "inputs": {},
        }

    return _make


@pytest.fixture
def mock_langsmith_runs_pages() -> Callable[..., None]:
    """Factory: register POST /runs/query returning each page in order, cursor-chained."""

    def _mock(respx_mock: Any, pages: list[list[dict[str, Any]]]) -> None:
        responses = []
        for i, runs in enumerate(pages):
            body: dict[str, Any] = {"runs": runs}
            if i < len(pages) - 1:
                body["cursors"] = {"next": f"cursor-{i + 1}"}
            responses.append(httpx.Response(200, json=body))
        respx_mock.post(f"{LANGSMITH_API_URL}/runs/query").mock(side_effect=responses)

    return _mock


@pytest.fixture
def mock_langsmith_runs_sequence() -> Callable[..., None]:
    """Factory: register POST /runs/query returning exactly `responses`, for error-path tests."""

    def _mock(respx_mock: Any, responses: list[httpx.Response]) -> None:
        respx_mock.post(f"{LANGSMITH_API_URL}/runs/query").mock(side_effect=responses)

    return _mock
