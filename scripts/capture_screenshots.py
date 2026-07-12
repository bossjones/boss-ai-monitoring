"""Capture dashboard screenshots from a LIVE app against the REAL database.

Run it against a running `bam serve` (it will not start one for you — the point is to photograph
the real thing, not a fixture-seeded stand-in):

    uv run bam serve                       # terminal 1
    uv run python scripts/capture_screenshots.py    # terminal 2

Writes PNGs to docs/img/dashboard/. If a page renders empty, that IS the result — the script says
so instead of quietly producing a pretty, meaningless image.

Chromium ships with the dev deps and is already installed; do not run `playwright install` here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parents[1] / "docs" / "img" / "dashboard"
VIEWPORT = {"width": 1440, "height": 900}


def _first_session_id() -> str | None:
    """Grab a real session id so /sessions/{id} shows an actual timeline, not a 404."""
    try:
        response = httpx.get(f"{BASE}/api/overview", timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  ! could not read /api/overview: {exc}")
        return None

    data = response.json()
    for key in ("recent_sessions", "sessions"):
        rows = data.get(key) or []
        if rows:
            return rows[0].get("session_id")
    return None


def main() -> int:
    try:
        httpx.get(BASE, timeout=5.0).raise_for_status()
    except httpx.HTTPError:
        print(f"No app at {BASE}. Start it first:  uv run bam serve")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    session_id = _first_session_id()

    pages = [
        ("overview", "/"),
        ("live", "/live"),
        ("costs", "/costs"),
    ]
    if session_id:
        pages.append(("session-detail", f"/sessions/{session_id}"))
    else:
        print("  ! no sessions in the DB — skipping the session-detail shot rather than faking it")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        for name, route in pages:
            # NOT `networkidle`: /live holds an SSE connection open forever, so the network is
            # never idle and the wait times out. domcontentloaded + a settle pause is correct.
            page.goto(f"{BASE}{route}", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1500)  # let htmx fragments render and the SSE feed connect
            dest = OUT / f"{name}.png"
            page.screenshot(path=str(dest), full_page=True)

            # A screenshot of an empty page is not evidence. Say so.
            body = page.inner_text("body")
            verdict = "OK" if len(body.strip()) > 120 else "LOOKS EMPTY"
            print(f"  {verdict:11s} {route:28s} -> {dest.relative_to(OUT.parents[2])}")
        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
