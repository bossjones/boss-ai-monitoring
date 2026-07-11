"""RED-first tests for the `bam` console script.

`bam serve` runs ONE FastAPI app on TWO uvicorn binds (dashboard + OTLP) in one asyncio loop.
`bam config db-path` prints the RESOLVED DuckDB path — GATE's duckdb checks depend on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from boss_ai_monitoring import cli


def test_console_script_entry_point_is_declared() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = pyproject.read_text()

    assert 'bam = "boss_ai_monitoring.cli:main"' in text


def test_config_db_path_prints_resolved_path(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAM_STORE__DB_PATH", str(tmp_path / "resolved.duckdb"))

    exit_code = cli.main(["config", "db-path"])

    printed = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert printed == str(tmp_path / "resolved.duckdb")
    assert Path(printed).is_absolute()


def test_config_db_path_resolves_home_relative_paths(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BAM_STORE__DB_PATH", "~/tilde.duckdb")

    cli.main(["config", "db-path"])

    printed = capsys.readouterr().out.strip()
    assert "~" not in printed
    assert printed == str(Path.home() / "tilde.duckdb")


def test_serve_starts_two_uvicorn_binds_on_one_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard :8000 and OTLP :4318 are two Server instances over the SAME app object."""
    started: list[dict[str, Any]] = []

    class FakeServer:
        def __init__(self, config: Any) -> None:
            self.config = config

        async def serve(self, sockets: Any = None) -> None:
            started.append(
                {"app": self.config.app, "host": self.config.host, "port": self.config.port}
            )

    monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)

    exit_code = cli.main(["serve"])

    assert exit_code == 0
    assert len(started) == 2, "serve must run exactly two binds"
    ports = sorted(entry["port"] for entry in started)
    assert ports == [4318, 8000]
    assert {entry["host"] for entry in started} == {"127.0.0.1"}
    apps = {id(entry["app"]) for entry in started}
    assert len(apps) == 1, "both binds must serve the SAME app object"


def test_serve_honors_configured_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    ports: list[int] = []

    class FakeServer:
        def __init__(self, config: Any) -> None:
            self.config = config

        async def serve(self, sockets: Any = None) -> None:
            ports.append(self.config.port)

    monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)
    monkeypatch.setenv("BAM_SERVER__DASHBOARD_PORT", "8111")
    monkeypatch.setenv("BAM_SERVER__OTLP_PORT", "4319")

    cli.main(["serve"])

    assert sorted(ports) == [4319, 8111]


def test_unknown_command_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["bogus"])

    assert excinfo.value.code != 0
