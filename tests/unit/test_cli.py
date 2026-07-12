"""RED-first tests for the `bam` console script.

`bam serve` runs ONE FastAPI app on TWO uvicorn binds (dashboard + OTLP) in one asyncio loop.
`bam config db-path` prints the RESOLVED DuckDB path — GATE's duckdb checks depend on it.
"""

from __future__ import annotations

import asyncio
import logging
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


def test_snapshot_prints_a_bare_path_so_it_composes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`duckdb "$(uv run bam snapshot)" ...` only works if stdout is JUST the path."""
    monkeypatch.setenv("BAM_STORE__DB_PATH", str(tmp_path / "bam.duckdb"))
    monkeypatch.setenv("BAM_STORE__SNAPSHOT_DIR", str(tmp_path / "snaps"))

    exit_code = cli.main(["snapshot"])

    printed = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert Path(printed).is_absolute()
    assert Path(printed).exists()
    assert printed.count("\n") == 0


def test_snapshot_falls_back_to_local_when_the_app_is_not_running(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """App down: don't fail, just snapshot in-process. Nothing is listening on the port here."""
    monkeypatch.setenv("BAM_STORE__DB_PATH", str(tmp_path / "bam.duckdb"))
    monkeypatch.setenv("BAM_STORE__SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.setenv("BAM_SERVER__DASHBOARD_PORT", "9")  # discard port, guaranteed refused

    exit_code = cli.main(["snapshot"])

    assert exit_code == 0
    assert Path(capsys.readouterr().out.strip()).exists()


def test_snapshot_asks_the_running_app_when_one_is_up(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """App up: the CLI must delegate to it — the app holds the only usable connection."""
    served = tmp_path / "from-the-app.duckdb"
    served.write_text("")

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"path": str(served), "rows": 7}

        @staticmethod
        def raise_for_status() -> None:
            return None

    def fake_post(url: str, **_: Any) -> FakeResponse:
        assert url.endswith("/api/snapshot")
        return FakeResponse()

    monkeypatch.setenv("BAM_STORE__DB_PATH", str(tmp_path / "bam.duckdb"))
    monkeypatch.setattr(cli.httpx, "post", fake_post)

    exit_code = cli.main(["snapshot"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == str(served)


class TestServeStartsBackgroundWork:
    """`bam serve` must actually RUN the ingest loops and the trailing jobs.

    Found during close-out: the JSONL scanner, the LangSmith poller and the JobScheduler were all
    implemented, tested, and never started — `_serve_both` only launched the two uvicorn Servers.
    OTLP still worked (it is an HTTP route), so the app LOOKED fine while two of its three ingest
    sources were dead in production and the trailing jobs never ran once (`drift: unknown` in the
    provenance footer was the visible symptom nobody chased).
    """

    def test_serve_launches_ingest_loops_and_the_job_scheduler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: list[str] = []

        class FakeServer:
            def __init__(self, config: Any) -> None:
                self.config = config

            async def serve(self) -> None:
                started.append(f"uvicorn:{self.config.port}")

        async def fake_jsonl(*_a: Any, **_k: Any) -> None:
            started.append("jsonl")

        async def fake_langsmith(*_a: Any, **_k: Any) -> None:
            started.append("langsmith")

        async def fake_jobs(*_a: Any, **_k: Any) -> None:
            started.append("jobs")

        monkeypatch.setenv("BAM_STORE__DB_PATH", str(tmp_path / "bam.duckdb"))
        monkeypatch.setenv("BAM_INGEST__LANGSMITH_PROJECT", "proj")
        monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)
        monkeypatch.setattr(cli, "_run_jsonl_scanner", fake_jsonl)
        monkeypatch.setattr(cli, "_run_langsmith_poller", fake_langsmith)
        monkeypatch.setattr(cli, "_run_jobs", fake_jobs)

        assert cli.main(["serve"]) == 0

        assert "jsonl" in started, "the JSONL scanner never started"
        assert "langsmith" in started, "the LangSmith poller never started"
        assert "jobs" in started, "the trailing job scheduler never started"
        assert "uvicorn:8000" in started and "uvicorn:4318" in started

    def test_a_crashing_background_loop_does_not_take_the_server_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ingest is best-effort. A dead poller must not kill the dashboard."""
        served: list[str] = []

        class FakeServer:
            def __init__(self, config: Any) -> None:
                self.config = config

            async def serve(self) -> None:
                served.append(f"uvicorn:{self.config.port}")

        async def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("poller exploded")

        monkeypatch.setenv("BAM_STORE__DB_PATH", str(tmp_path / "bam.duckdb"))
        monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)
        monkeypatch.setattr(cli, "_run_jsonl_scanner", boom)
        monkeypatch.setattr(cli, "_run_langsmith_poller", boom)
        monkeypatch.setattr(cli, "_run_jobs", boom)

        assert cli.main(["serve"]) == 0
        assert served == ["uvicorn:8000", "uvicorn:4318"]


class TestJobResultsArePersisted:
    """The trailing jobs must RECORD their runs, not just execute them.

    `_run_jobs` built the scheduler without `persist=`, so the jobs ran and their results went
    nowhere: no `job_run` events, so `web/queries.py`'s drift lookup found nothing and the
    provenance footer read `drift: unknown` forever. `jobs/live.py::persist_job_status` already
    existed for exactly this — its own docstring spells out the wiring.
    """

    @pytest.mark.asyncio
    async def test_a_scheduler_pass_writes_a_job_run_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import duckdb

        from boss_ai_monitoring.config import load_settings
        from boss_ai_monitoring.store.writer import get_writer

        db = tmp_path / "bam.duckdb"
        monkeypatch.setenv("BAM_STORE__DB_PATH", str(db))
        settings = load_settings()

        scheduler = cli._build_job_scheduler(settings)
        await scheduler.run_once()
        get_writer(settings).close()  # flush + release the lock so we can read it back

        conn = duckdb.connect(str(db), read_only=True)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT json_extract_string(payload, '$.name') FROM events "
                    "WHERE event_type = 'job_run'"
                ).fetchall()
            }
        finally:
            conn.close()

        assert "drift_check" in names, "the drift job ran but its result was never persisted"


class TestLangsmithLoudOmission:
    """specs/outstanding.md P1(b): an unset LangSmith project must be LOUD.

    The poller starting only on `BAM_INGEST__LANGSMITH_PROJECT` is deliberate (G4 — config.py is
    the only env reader; the ambient LANGSMITH_PROJECT is NOT aliased). What was a defect is the
    single log.info nobody reads: the feature looked broken with no visible explanation.
    """

    def test_poller_skip_logs_a_warning_naming_the_env_var(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from boss_ai_monitoring.config import BamSettings

        monkeypatch.delenv("BAM_INGEST__LANGSMITH_PROJECT", raising=False)
        settings = BamSettings()
        assert settings.ingest.langsmith_project is None

        with caplog.at_level(logging.WARNING, logger="bam"):
            asyncio.run(cli._run_langsmith_poller(settings))

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("BAM_INGEST__LANGSMITH_PROJECT" in r.getMessage() for r in warnings), (
            "the unset-project skip must WARN and name the exact env var to set"
        )
