# The agent frontend-improvement loop

Phase 7's deliverable: a coding agent can iterate on this dashboard's UI on its own, because it's
server-rendered HTML/htmx with no build step — edit a template or `static/style.css`, reload,
look. This doc is the loop itself, worked once end-to-end below.

## The loop

1. **Start the dev server** against a local DB with some real data in it:
   ```bash
   BAM_STORE__DB_PATH=/tmp/bam-dev.duckdb BAM_SERVER__DASHBOARD_PORT=18000 BAM_SERVER__OTLP_PORT=18318 \
     just dev
   ```
   (Override the ports if 8000/4318 are already in use by another `bam serve`.) Seed it with a
   couple of the OTLP fixtures so panels aren't showing only empty states:
   ```bash
   curl -X POST http://127.0.0.1:18318/v1/logs -d @tests/fixtures/otlp/api_request.json
   curl -X POST http://127.0.0.1:18318/v1/logs -d @tests/fixtures/otlp/tool_result.json
   ```
2. **Open the dashboard and screenshot it.** Playwright is the default (already a dev dependency,
   scriptable, chromium pre-installed — do NOT run `playwright install`, it silently no-ops under
   this repo's rtk rewrite, BL-04):
   ```python
   from playwright.sync_api import sync_playwright
   with sync_playwright() as p:
       browser = p.chromium.launch()
       page = browser.new_page(viewport={"width": 1280, "height": 900})
       page.goto("http://127.0.0.1:18000/")
       page.wait_for_selector("#overview-panel")
       page.screenshot(path="/tmp/before.png", full_page=True)
       browser.close()
   ```
   If your session supports the Claude Code in-app browser / `preview_start`, that works too —
   don't block on it, Playwright is the fallback of record for this run.
3. **Critique against [`design-tokens.md`](design-tokens.md).** Look for: color used decoratively
   instead of semantically (cost isn't amber, a live signal isn't teal); text using the wrong font
   family (data in sans, prose in mono); anything that should be visible but reads as blank or
   broken.
4. **Edit** `web/templates/*.html` and/or `web/static/style.css` directly. No npm, no build step —
   htmx and its extensions are vendored as static files.
5. **Reload.** `uv run bam serve` isn't running with `--reload`, so restart it (or add `--reload`
   to a local `uvicorn` invocation while iterating) — a template/CSS edit needs a process restart
   or reload to take effect, since Jinja2Templates and StaticFiles both read from disk on each
   request but Python module state (routes) doesn't need to change for a pure template/CSS edit.
6. **Re-screenshot and compare.** Confirm the specific defect from step 3 is gone and nothing else
   regressed — the playwright e2e suite (`uv run pytest tests/e2e -q`) staying green is the
   regression net for the edit.

## Worked example (the one required real iteration)

**Screenshot** — `docs/img/agent-loop/overview-before.png`. The Overview page's "Cost — last 14
days" sparkline: one bar (today, $0.04) is visible; the other 13 (all $0.00, no data ingested
those days) render as a barely-there ~1px sliver. The chart reads as broken, not "no data."

**Critique against design-tokens.md**: two things, once you look for them —

1. The sparkline fills with `--bam-teal` (live/healthy), but this is a **cost** chart — the token
   sheet's own rule ("amber — cost — every dollar figure") says it should be `--bam-amber`.
2. `.sparkline__bar`'s height was `calc(max(var(--value), 0.02) * 3rem)` — the 0.02 floor is
   itself only ~1px of the 3rem (48px) bar height, so a $0 day is visually indistinguishable from
   "this bar doesn't exist."

**Edit** — `web/static/style.css`, `.sparkline__bar`:
```diff
- background: var(--bam-teal);
- height: calc(max(var(--value), 0.02) * 3rem);
+ background: var(--bam-amber);
+ height: max(calc(var(--value) * 3rem), 3px);
```
`max()` now compares the *scaled pixel height* against a literal `3px` floor, rather than baking
a tiny fractional minimum into `--value` itself — a $0 day gets an honest, visible baseline tick
instead of disappearing.

**Reload and re-screenshot** — `docs/img/agent-loop/overview-after.png`. Every day in the 14-day
window now renders as a legible amber bar of consistent minimum height; today's non-zero cost
still reads as taller. The chart looks like data again, and the color now matches its meaning.

**Regression check**: `uv run pytest tests/e2e -q` stayed green after the edit (no route or
template structure changed, only CSS).
