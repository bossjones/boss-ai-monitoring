"""Playwright smoke test booting the REAL server (web.md Phase 6 acceptance).

Loads `/`, opens `/live`, POSTs an OTLP fixture at `/v1/logs`, and asserts the posted event
appears on the `/live` page via SSE within 5s -- the regression net for the whole Wave 3 wiring
(create_app -> otlp-mount -> store -> _poll_events -> browser).

Chromium is already installed and verified (BL-04) -- do not run `playwright install` here.

Was blocked by OQ-04 (`connect_read_only()` vs a live in-process writer) until store landed the
writer-aware `.cursor()` fallback in `store/writer.py::connect_read_only` -- verified green here
against the real single-process `bam serve` topology (otlp mounted per LT-01).
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from boss_ai_monitoring.web.app import create_app

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "otlp" / "api_request.json"
_STARTUP_TIMEOUT_S = 10
_SSE_ASSERTION_TIMEOUT_MS = 5000


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(settings) -> Iterator[str]:
    """Boot `create_app(settings)` on a real socket via uvicorn, in a background thread."""
    port = _free_port()
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    started = False
    while time.monotonic() < deadline:
        try:
            httpx.get(base_url + "/", timeout=0.5)
            started = True
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    if not started:
        raise RuntimeError("live_server did not start within the timeout")

    yield base_url

    server.should_exit = True
    thread.join(timeout=_STARTUP_TIMEOUT_S)


def test_live_feed_shows_a_posted_otlp_event_via_sse(live_server: str) -> None:
    fixture_payload = json.loads(_FIXTURE_PATH.read_text())

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()

            page.goto(f"{live_server}/")
            assert page.locator("#overview-panel").count() == 1

            page.goto(f"{live_server}/live")
            assert page.locator("#live-panel").count() == 1

            response = httpx.post(f"{live_server}/v1/logs", json=fixture_payload, timeout=5)
            assert response.status_code == 200

            page.wait_for_function(
                "document.getElementById('live-sse-ticker').textContent.includes('api_request')",
                timeout=_SSE_ASSERTION_TIMEOUT_MS,
            )
        finally:
            browser.close()
