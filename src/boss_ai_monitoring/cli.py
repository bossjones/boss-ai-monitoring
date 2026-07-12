"""The `bam` console script.

`bam serve` is the decided serving model: ONE FastAPI app (web/app.py, with the OTLP router
mounted) served on TWO uvicorn binds -- dashboard (:8000) and OTLP (:4318) -- as two Server
instances in a single asyncio loop. Nobody else in the codebase wires ports.

`bam config db-path` prints the RESOLVED DuckDB path. Shell checks must use
`duckdb "$(uv run bam config db-path)"`; a bare $BAM_DB_PATH is unset in most shells and would
silently open an empty database.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import httpx
import uvicorn
from fastapi import FastAPI

from boss_ai_monitoring import __version__
from boss_ai_monitoring.config import BamSettings, load_settings
from boss_ai_monitoring.ingest import jsonl as jsonl_ingest
from boss_ai_monitoring.ingest import langsmith_poll as langsmith_ingest
from boss_ai_monitoring.jobs.live import build_live_callables
from boss_ai_monitoring.jobs.scheduler import JobScheduler
from boss_ai_monitoring.store.writer import get_writer, snapshot

log = logging.getLogger("bam")

# CONTRACT for the web pane: web/app.py exposes `create_app(settings: BamSettings) -> FastAPI`
# (with the otlp router mounted inside it). Until that lands, serve falls back to a placeholder
# app so the two-bind wiring is testable -- the fallback is loud, never silent.
WEB_APP_MODULE = "boss_ai_monitoring.web.app"


def build_app(settings: BamSettings) -> FastAPI:
    """The ONE app object both binds serve.

    Resolved at runtime rather than imported statically: web/app.py is owned by another pane and
    does not exist during the scaffold wave. The fallback is loud, never silent.
    """
    try:
        module = importlib.import_module(WEB_APP_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != WEB_APP_MODULE:
            raise
        log.warning("%s not present yet - serving a placeholder app", WEB_APP_MODULE)
        placeholder = FastAPI(title="boss-ai-monitoring (placeholder)", version=__version__)

        @placeholder.get("/healthz")
        def healthz() -> dict[str, str]:
            return {"status": "scaffold", "version": __version__}

        return placeholder

    create_app: Callable[[BamSettings], FastAPI] = module.create_app
    return create_app(settings)


async def _run_jsonl_scanner(settings: BamSettings) -> None:
    """Incremental reader over ~/.claude/projects — history and gap-fill."""
    await jsonl_ingest.run_forever(
        settings.ingest.claude_projects_dir,
        get_writer(settings),
        interval_s=settings.ingest.jsonl_scan_interval_s,
    )


async def _run_langsmith_poller(settings: BamSettings) -> None:
    """Cursor-based LangSmith poll. Degrades gracefully with no project/key configured."""
    project = settings.ingest.langsmith_project
    if not project:
        log.info("langsmith: no project configured — poller not started")
        return
    await langsmith_ingest.run_forever(
        project,
        get_writer(settings),
        interval_s=settings.ingest.langsmith_poll_interval_s,
    )


async def _run_jobs(settings: BamSettings) -> None:
    """Trailing quality jobs (correction scan, OTel-vs-JSONL drift, error classification)."""
    scheduler = JobScheduler.from_settings(
        settings.jobs,
        build_live_callables(settings.store.db_path),
    )
    await scheduler.run_forever()


async def _supervise(name: str, coro: Awaitable[None]) -> None:
    """Run a background loop so that its death cannot take the servers down with it.

    Ingest and the trailing jobs are best-effort: a LangSmith outage or a malformed transcript
    must degrade the data, never the dashboard. Each loop already retries internally; this is the
    outer net for anything that escapes.
    """
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("%s: background loop died — the server keeps running without it", name)


async def _serve_both(app: FastAPI, settings: BamSettings) -> None:
    """Two uvicorn Servers, one loop, same app object — plus the background ingest/jobs loops.

    The servers are the foreground work: when they return, we cancel everything else. Without
    this wiring `bam serve` ingests OTLP only (it is an HTTP route) while the JSONL scanner, the
    LangSmith poller and the job scheduler sit implemented-but-never-started — the app looks
    healthy with two of its three sources dead.
    """
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.server.dashboard_bind,
                port=settings.server.dashboard_port,
                log_level="info",
            )
        ),
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.server.otlp_bind,
                port=settings.server.otlp_port,
                log_level="info",
            )
        ),
    ]

    background = [
        asyncio.create_task(_supervise("jsonl", _run_jsonl_scanner(settings))),
        asyncio.create_task(_supervise("langsmith", _run_langsmith_poller(settings))),
        asyncio.create_task(_supervise("jobs", _run_jobs(settings))),
    ]
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)


def _cmd_serve(_args: argparse.Namespace) -> int:
    settings = load_settings()
    app = build_app(settings)
    log.info(
        "serving dashboard on %s:%s and OTLP on %s:%s",
        settings.server.dashboard_bind,
        settings.server.dashboard_port,
        settings.server.otlp_bind,
        settings.server.otlp_port,
    )
    asyncio.run(_serve_both(app, settings))
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.config_topic == "db-path":
        print(settings.store.db_path)
        return 0
    print(settings.model_dump_json(indent=2))
    return 0


def _cmd_snapshot(_args: argparse.Namespace) -> int:
    """Print the path to a consistent copy of the DB — readable WHILE `bam serve` runs.

    DuckDB's file lock is exclusive cross-process (OQ-05), so an outside `duckdb`/marimo process
    cannot open the live DB at all. Ask the running app (it owns the only usable connection); if
    nothing is listening, do it in-process instead.

    Prints ONLY the path, so it composes:
        duckdb "$(uv run bam snapshot)" "SELECT source, count(*) FROM events GROUP BY 1"
    """
    settings = load_settings()
    url = f"http://{settings.server.dashboard_bind}:{settings.server.dashboard_port}/api/snapshot"

    try:
        response = httpx.post(url, timeout=30.0)
        response.raise_for_status()
        print(response.json()["path"])
        return 0
    except (httpx.ConnectError, httpx.ConnectTimeout):
        log.info("no app listening on %s — snapshotting in-process instead", url)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    dest = settings.store.snapshot_dir / f"bam-{stamp}.duckdb"
    print(snapshot(settings.store.db_path, dest))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bam", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"bam {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="serve the dashboard (:8000) and OTLP (:4318)")
    serve.set_defaults(func=_cmd_serve)

    config = subcommands.add_parser("config", help="inspect resolved configuration")
    config.add_argument(
        "config_topic",
        nargs="?",
        default="show",
        choices=["show", "db-path"],
        help="db-path prints the resolved DuckDB path",
    )
    config.set_defaults(func=_cmd_config)

    snap = subcommands.add_parser(
        "snapshot",
        help="print the path to a consistent DB copy, readable while `bam serve` is running",
    )
    snap.set_defaults(func=_cmd_snapshot)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
