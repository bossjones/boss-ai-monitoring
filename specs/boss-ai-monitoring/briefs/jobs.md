# jobs.md — ⚙️ jobs brief (Phase 8: Trailing Quality Jobs + Phase 9: marimo, Docker, Docs)

> Derived from `../boss-ai-monitoring.html` (canonical) on 2026-07-11. Read `shared.md` first.
> If this conflicts with the HTML or observed behavior, the evidence wins — file an OQ.

**You own:** `src/boss_ai_monitoring/jobs/**`, `notebooks/explore.py`, `Dockerfile`,
`compose.yaml`, `tests/unit/jobs/**`, `tests/integration/test_docker.py`.

## Phase 8: Trailing Quality Jobs (off the critical path)

Watcher's lesson applied: heavy or judgmental analysis runs as background jobs over stored data —
**never blocking ingest or the agent**. Three jobs, all pure-SQL/heuristic in v1 (LLM-judged
variants are explicitly future work — Anthropic's warning: LLM-generated definitions "encoded the
very ambiguities we were trying to eliminate").

### 1. Jobs (TDD)

- **Correction-language scan**: heuristic detection over transcript user messages — signals like
  "no, that's wrong", "undo that", a re-prompt within 60s of a failed `tool_result` → per-session
  correction score (Anthropic's correction-mining analog). Surfaced on session pages.
- **Drift self-check**: daily job comparing OTel-derived vs JSONL-derived session/token totals
  per day; drift > threshold writes an alert row → dashboard badge (the "silent failure"
  sanity-check pattern). This is the standing mitigation for spec RISK #1 (JSONL format drift).
- **Error classification rollup**: group `api_error` / `tool_result.error_type` into a taxonomy
  table feeding a dashboard panel (Sniffly's most-loved feature).
- **Scheduler**: asyncio periodic runner with jitter, per-job enable/disable from config
  (`jobs` section), last-run status persisted and shown in the provenance footer.

> FENCE: the provenance footer is 🖥 web's rendering of your data — file a backlog item with the
> query/shape (job name, last run ts, status); web wires the footer. Don't edit web files.

### Phase 8 testing strategy

Pytest over seeded fixture data with known expected outputs (e.g. a fixture set with 2
corrections and 1 drift day must yield exactly those rows). Edge cases the spec requires:

- empty data
- all-sources-agree (no alert row)
- **job crash isolation** — one failing job never kills the scheduler or ingest

### Phase 8 acceptance

- `uv run pytest tests/unit/jobs -q` — all jobs green including crash-isolation test.
  *(Scaffold hermetically against fixture event rows during Wave 1 shadow work; wire to the live
  `events` table in Wave 3.)*
- `just check` — clean.

## Phase 9: marimo Notebook, Docker Packaging, Docs

### 1. marimo exploration notebook

- Write `notebooks/explore.py` (the marimo dependency was already added by the lead in Wave 0 —
  pyproject.toml/uv.lock are lead-owned, do NOT run `uv add`): **read-only DuckDB connection**
  (avoids the single-writer conflict with the running app), reactive SQL cells for "why did
  Tuesday cost $9?" digging — per-day drill-down, per-session breakdown, token-class mix, tool
  failure explorer.
- Propose README docs (via backlog): `uvx marimo edit notebooks/explore.py` and `marimo run` app
  mode.

### 2. Docker packaging

- Multi-stage `Dockerfile` (uv-based build, slim runtime).
- `compose.yaml`: ONE service; ports **8000** (dashboard) + **4318** (OTLP); a volume for the
  DuckDB file; a **read-only** mount for `~/.claude/projects`; document the macOS
  `host.docker.internal` nuance for Claude Code → container OTLP delivery.
- Local `uv run` stays the documented primary dev path; Docker is the deployment convenience.
- **Fast-loop discipline (user directive):** bring the container up the MINIMUM number of times —
  ideally once when the Dockerfile/compose first work, once at GATE. All code iteration happens
  against `uv run bam serve` from the repo (reload, no image rebuild). If compose ever grows
  external services beyond the app itself, leave them up in the background and point the local
  `uv run` app at them via `BAM_*` config env vars — never rebuild the app image to test a code
  change. Document this dev-loop split in the README proposal.

### 3. Docs (proposed via backlog — README is lead-owned)

File one backlog item containing the full README content for the lead to fold in: quickstart
(enable CC telemetry env vars → `uv run bam serve` → open dashboard), architecture diagram,
LangSmith plugin setup (`/plugin marketplace add langchain-ai/langsmith-claude-code-plugins` +
`TRACE_TO_LANGSMITH`/`CC_LANGSMITH_*` vars), privacy notes (content-capture OTel flags stay off
unless opted in), and the back-reference link to
`specs/boss-ai-monitoring/boss-ai-monitoring.html`.

### Phase 9 testing strategy

Build-and-boot validation: the container must serve BOTH ports and pass the same E2E fixture
round-trip as local dev. Edge cases: missing volume mounts (clear error), notebook opened while
the app is writing (read-only mode holds).

### Phase 9 acceptance

- `docker compose up --build -d && curl -sf localhost:8000/ && curl -sf -X POST
  localhost:4318/v1/logs -H 'Content-Type: application/json' -d
  @tests/fixtures/otlp/api_request.json` — container round-trip works.
- `uvx marimo run notebooks/explore.py` — opens read-only against a live DB without errors.
- `just check` — clean.

### Docker-build patience rule

Base-image pulls take a few minutes — that's normal. Poll for actual forward progress (new layer
lines); if genuinely stuck (no new output 5+ min AND no process), kill, check disk space and
Docker Desktop state, retry once, then file an OQ rather than looping silently.
