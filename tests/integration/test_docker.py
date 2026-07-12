"""Docker packaging round-trip (jobs.md Phase 9 acceptance).

Builds the real image via `docker compose`, boots the `app` service, and confirms both
uvicorn binds are reachable: `/` on the dashboard port and `POST /v1/logs` on the OTLP port.
Marked `docker` and deselected by default (see pyproject.toml addopts) -- run via
`just test-docker`. Runs `docker compose` with its own `-p` project name so it can't collide
with a developer's own `docker compose up` stack, and always tears down in `finally`.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

if shutil.which("docker") is None:
    pytest.skip("docker is not installed", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_PATH = _REPO_ROOT / "tests" / "fixtures" / "otlp" / "api_request.json"
_PROJECT_NAME = "bam-integration-test"
_STARTUP_TIMEOUT_S = 120


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", _PROJECT_NAME, *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.mark.docker
def test_compose_round_trip_serves_both_ports() -> None:
    _ = _compose("up", "--build", "-d")
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        started = False
        while time.monotonic() < deadline:
            try:
                if httpx.get("http://localhost:8000/", timeout=1.0).status_code < 400:
                    started = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1.0)
        if not started:
            raise RuntimeError("dashboard port did not become ready within the timeout")

        otlp_response = httpx.post(
            "http://localhost:4318/v1/logs",
            content=_FIXTURE_PATH.read_bytes(),
            headers={"Content-Type": "application/json"},
            timeout=5.0,
        )
        assert otlp_response.status_code < 400
    finally:
        _ = _compose("down", "-v")
