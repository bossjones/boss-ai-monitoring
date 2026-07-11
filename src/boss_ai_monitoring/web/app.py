"""FastAPI app factory — the BL-02 contract: `create_app(settings) -> FastAPI`.

`cli.py` (lead-owned, `bam serve`) resolves `create_app` at runtime and serves the returned app
object on both binds (:8000 dashboard, :4318 OTLP) — see `boss_ai_monitoring.cli.build_app`.
Nobody outside `cli.py` wires ports.

Wave 1 SHADOW: routes render against `get_connection`, a dependency tests override with a fixture
DuckDB connection (`tests/unit/web/conftest.py`). Production wiring (opening the real store file)
is written but unexercised until Wave 3 -- this module does not import `boss_ai_monitoring.store`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from boss_ai_monitoring.config import BamSettings
from boss_ai_monitoring.web import queries

_WEB_DIR = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def _is_fragment_request(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def get_connection(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    """Production data source: a read-only connection to the configured DuckDB file.

    Wave 1 SHADOW: not exercised by tests -- `app.dependency_overrides[get_connection]` supplies
    a fixture connection instead. Wave 3 wires this to the live store.
    """
    settings: BamSettings = request.app.state.settings
    conn = duckdb.connect(str(settings.store.db_path), read_only=True)
    try:
        yield conn
    finally:
        conn.close()


Connection = Annotated[duckdb.DuckDBPyConnection, Depends(get_connection)]


def create_app(settings: BamSettings) -> FastAPI:
    app = FastAPI(title="boss-ai-monitoring")
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    # otlp-mount
    # 📡 otlp's `get_router() -> APIRouter` is mounted here, once the lead issues the loan ticket
    # (web.md "The ONE recorded handoff"). Empty in Wave 1 -- do not fill this in without one.
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
        async def _events() -> AsyncIterator[dict[str, str]]:
            # Wave 1: no live event bus yet. Wave 3 wires ingest-flush -> SSE push (web.md).
            yield {"event": "heartbeat", "data": "waiting-for-wave-3"}

        return EventSourceResponse(_events())

    return app
