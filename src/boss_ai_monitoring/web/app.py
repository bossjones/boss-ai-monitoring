"""FastAPI app factory — the BL-02 contract: `create_app(settings) -> FastAPI`.

`cli.py` (lead-owned, `bam serve`) resolves `create_app` at runtime and serves the returned app
object on both binds (:8000 dashboard, :4318 OTLP) — see `boss_ai_monitoring.cli.build_app`.
Nobody outside `cli.py` wires ports.

Wave 3: routes read through `store.connect_read_only` (LT-01 lifted the Wave 1 restriction on
importing `boss_ai_monitoring.store`). web NEVER opens a write connection (G5) -- if the DuckDB
file doesn't exist yet (no writer has flushed a first batch), `get_connection` yields `None` and
every panel renders its empty state instead of touching duckdb at all. Unit tests override
`get_connection` with a fixture connection (`tests/unit/web/conftest.py`); production wiring is
exercised for real by `tests/e2e/test_dashboard.py`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from boss_ai_monitoring.config import BamSettings
from boss_ai_monitoring.store.writer import connect_read_only
from boss_ai_monitoring.store.writer import snapshot as snapshot_db_file
from boss_ai_monitoring.web import queries

_WEB_DIR = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))

_SSE_POLL_INTERVAL_S = 0.5


class _DisconnectCheck(Protocol):
    """The one bit of `Request` the poll loop needs -- lets tests drive it without a real ASGI
    request/response cycle (a live `TestClient` stream never signals disconnect for an endpoint
    that polls forever, so unit tests exercise this loop directly instead)."""

    async def is_disconnected(self) -> bool: ...


async def _poll_events(
    settings: BamSettings, request: _DisconnectCheck
) -> AsyncIterator[dict[str, str]]:
    """Poll the read-only store for new rows and yield each as an SSE message payload.

    OTel logs export every ~5s (shared.md) and nothing here holds a lock the single writer
    needs, so a short poll loop over `connect_read_only` is realtime enough for the live feed
    without any cross-module event bus.
    """
    last_ts: datetime | None = None
    while not await request.is_disconnected():
        if settings.store.db_path.exists():
            conn = connect_read_only(settings.store.db_path)
            try:
                query = (
                    "SELECT event_id, ts, source, event_type, session_id, tool_name, cost_usd "
                    "FROM events"
                )
                params: list[object] = []
                if last_ts is not None:
                    query += " WHERE ts > ?"
                    params.append(last_ts)
                query += " ORDER BY ts ASC LIMIT 200"
                rows = conn.execute(query, params).fetchall()
            finally:
                conn.close()
            for row in rows:
                last_ts = row[1]
                payload = {
                    "event_id": row[0],
                    "ts": row[1].isoformat(),
                    "source": row[2],
                    "event_type": row[3],
                    "session_id": row[4],
                    "tool_name": row[5],
                    "cost_usd": row[6],
                }
                yield {"event": "message", "data": json.dumps(payload)}
        await asyncio.sleep(_SSE_POLL_INTERVAL_S)


def _is_fragment_request(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def get_connection(request: Request) -> Iterator[duckdb.DuckDBPyConnection | None]:
    """A read-only connection to the configured DuckDB file, or `None` if it doesn't exist yet.

    web never creates the file (G5: read-only, period) -- the writer creates it on first flush.
    Tests override this with `app.dependency_overrides[get_connection]`.
    """
    settings: BamSettings = request.app.state.settings
    if not settings.store.db_path.exists():
        yield None
        return
    conn = connect_read_only(settings.store.db_path)
    try:
        yield conn
    finally:
        conn.close()


Connection = Annotated[duckdb.DuckDBPyConnection | None, Depends(get_connection)]


def create_app(settings: BamSettings) -> FastAPI:
    app = FastAPI(title="boss-ai-monitoring")
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    # otlp-mount
    # LT-01 (lead-issued loan ticket, Wave 3): mount 📡 otlp's router into the ONE app object.
    # Scope of the loan is this mount call only -- router internals stay owned by ingest/otlp.py.
    from boss_ai_monitoring.ingest.otlp import get_router as get_otlp_router

    app.include_router(get_otlp_router())
    # otlp-mount

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, conn: Connection) -> HTMLResponse:
        data = queries.get_overview(conn, now=datetime.now(UTC))
        template = (
            "partials/overview_fragment.html" if _is_fragment_request(request) else "overview.html"
        )
        return _TEMPLATES.TemplateResponse(
            request, template, {"overview": data, "provenance": data.provenance}
        )

    @app.get("/api/overview")
    def overview_json(conn: Connection) -> JSONResponse:
        data = queries.get_overview(conn, now=datetime.now(UTC))
        return JSONResponse(jsonable_encoder(data))

    @app.get("/live", response_class=HTMLResponse)
    def live(request: Request, conn: Connection) -> HTMLResponse:
        data = queries.get_live(conn, now=datetime.now(UTC))
        template = "partials/live_fragment.html" if _is_fragment_request(request) else "live.html"
        return _TEMPLATES.TemplateResponse(
            request, template, {"live": data, "provenance": data.provenance}
        )

    @app.get("/api/live")
    def live_json(conn: Connection) -> JSONResponse:
        data = queries.get_live(conn, now=datetime.now(UTC))
        return JSONResponse(jsonable_encoder(data))

    @app.get("/sessions/{session_id}", response_class=HTMLResponse)
    def session_detail(request: Request, session_id: str, conn: Connection) -> HTMLResponse:
        data = queries.get_session_detail(conn, session_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        template = (
            "partials/session_detail_fragment.html"
            if _is_fragment_request(request)
            else "session_detail.html"
        )
        return _TEMPLATES.TemplateResponse(
            request, template, {"session": data, "provenance": data.provenance}
        )

    @app.get("/api/sessions/{session_id}")
    def session_detail_json(session_id: str, conn: Connection) -> JSONResponse:
        data = queries.get_session_detail(conn, session_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        return JSONResponse(jsonable_encoder(data))

    @app.get("/costs", response_class=HTMLResponse)
    def costs(request: Request, conn: Connection) -> HTMLResponse:
        data = queries.get_costs(conn)
        template = "partials/costs_fragment.html" if _is_fragment_request(request) else "costs.html"
        return _TEMPLATES.TemplateResponse(
            request, template, {"costs": data, "provenance": data.provenance}
        )

    @app.get("/api/costs")
    def costs_json(conn: Connection) -> JSONResponse:
        data = queries.get_costs(conn)
        return JSONResponse(jsonable_encoder(data))

    @app.get("/api/events/stream")
    async def events_stream(request: Request) -> EventSourceResponse:
        return EventSourceResponse(_poll_events(settings, request))

    @app.post("/api/snapshot")
    def snapshot_db() -> JSONResponse:
        """Hand out a consistent copy of the DB so it can be read WHILE we are serving.

        DuckDB's file lock is exclusive cross-process (OQ-05), so while this app holds the write
        connection no outside `duckdb`/marimo process can open the file at all. We hold that
        connection, so we are the only one who can produce a copy.

        The destination is chosen HERE and never taken from the caller: an endpoint that writes to
        a caller-supplied path is a file-write primitive, and binding localhost (G10) is not a
        reason to hand one out.
        """
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        dest = settings.store.snapshot_dir / f"bam-{stamp}.duckdb"

        written = snapshot_db_file(settings.store.db_path, dest)

        conn = duckdb.connect(str(written), read_only=True)
        try:
            row = conn.execute("SELECT count(*) FROM events").fetchone()
        finally:
            conn.close()
        return JSONResponse({"path": str(written), "rows": row[0] if row else 0})

    return app
