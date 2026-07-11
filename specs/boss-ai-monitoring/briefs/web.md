# web.md — 🖥 web brief (Phase 6: Dashboard + Phase 7: Agent Frontend-Improvement Loop)

> Derived from `../boss-ai-monitoring.html` (canonical) on 2026-07-11. Read `shared.md` first.
> If this conflicts with the HTML or observed behavior, the evidence wins — file an OQ.

**You own:** `src/boss_ai_monitoring/web/**` (`app.py`, `templates/*.html`,
`static/style.css`), `tests/e2e/test_dashboard.py`, `docs/AGENT_LOOP.md`,
`docs/design-tokens.md`, `docs/img/agent-loop/**`.

The single pane of glass. Server-rendered HTML with htmx partial swaps; realtime via one SSE
stream. Vanilla CSS matching a small design-token sheet — deliberately framework-free so an agent
can edit any view file directly (Phase 7 depends on this). htmx is vendored as ONE static file:
no npm, no build step.

## Phase 6: Views (TDD where testable — route/JSON tests; visual polish iterates in Phase 7)

Routes, exactly:

- **`/` Overview** — stat tiles (today's cost, tokens by class, active sessions, tool success
  rate), 14-day cost sparkline, recent sessions table.
- **`/live`** — SSE-driven live event feed (new events pushed on ingest flush) + active-session
  cards with running cost/duration.
- **`/sessions/{id}`** — per-task timeline: one row per `prompt_id` with wall-clock bar, cost,
  tokens, tool calls (success/fail), subagent/skill attribution; LangSmith deep-link when a
  matching trace exists.
- **`/costs`** — daily/weekly rollups; attribution table per model / agent / skill / project
  (the "+6% accuracy for +72% latency"-style tradeoff view).
- **`/api/events/stream`** — SSE via sse-starlette; plus JSON endpoints backing every panel.

Two invariants on every panel:

1. **htmx fragment + JSON twin** (the twin is what route tests assert against).
2. **Provenance/freshness footer**: source lineage (otlp/jsonl/langsmith) + `MAX(ts)` per source
   + drift-check badge (Anthropic trust pattern). Jobs' last-run status also surfaces here — ⚙️
   jobs files a backlog item with the query/shape; you wire the rendering.

### Phase 6 testing strategy

Route tests with `TestClient` against seeded DuckDB fixtures (assert JSON twins + key DOM markers
in HTML fragments). One playwright smoke test booting the REAL server: loads `/`, opens `/live`,
POSTs an OTLP fixture, asserts the event appears via SSE within 5s. Edge cases the spec requires:

- empty DB → all views render friendly empty states
- sessions with no `prompt_id` events
- very long sessions (pagination)

### Phase 6 acceptance

- `uv run pytest tests/unit/web -q` (route/JSON/fragment tests) — green.
  *(Scaffold hermetically against FIXTURE rows during Wave 1 shadow work; wire to the live store
  in Wave 3.)*
- `uv run pytest tests/e2e/test_dashboard.py -q` — SSE realtime smoke green.
- `just check` — clean.

### The ONE recorded handoff

📡 otlp exposes `get_router() -> APIRouter`; you mount it in `web/app.py` inside a marked
`# otlp-mount` block under a lead-issued LOAN TICKET. You own the mount call; otlp owns the
router internals.

## Phase 7: Agent Frontend-Improvement Loop

Deliverable = `docs/AGENT_LOOP.md` + one demonstrated iteration with before/after screenshots
committed under `docs/img/agent-loop/`.

- Write `docs/AGENT_LOOP.md`: start `just dev`; open the dashboard; screenshot; critique against
  the design-token sheet; edit `web/templates/` + `web/static/style.css`; uvicorn --reload picks
  it up; re-screenshot and compare.
- Write `docs/design-tokens.md` — the palette/typography contract the agent critiques against
  (same identity family as the spec: "signal on slate" — light engineering surface, deep slate
  ink `#1c2733`, teal `#0e7c7b` for live/healthy, amber `#b57614` for cost, coral `#c0504d` for
  failures; SF Mono/JetBrains Mono for data, system sans for prose).
- Run ONE real iteration end-to-end; commit before/after screenshots.

> RUN NOTE: browser access order for this run is Playwright first (already a dev dependency,
> scriptable), then a `cmux new-surface --type browser` if interactive iteration is needed. The
> spec's first choice (Claude Code in-app browser / `preview_start`) is available if your session
> supports it, but don't block on it.

### Phase 7 testing strategy

Process validation, not unit tests: proven by artifacts and by the playwright suite staying green
after your edits (the regression net for agent-made frontend changes).

- `ls docs/img/agent-loop/` — before/after screenshots exist and are referenced from
  AGENT_LOOP.md.
- `uv run pytest tests/e2e -q` — still green after the demonstrated edit.
