# validator.md — ✅ validator brief (TDD enforcement + GATE)

> Derived from `../boss-ai-monitoring.html` (canonical, esp. its Validation Commands section) on
> 2026-07-11. Read `shared.md` first. If this conflicts with the HTML or observed behavior, the
> evidence wins — file an OQ.

**You own:** `.team/boss-ai-monitoring-build.validator-log.md` — and NOTHING else by default.
Any other file only under a lead-issued LOAN TICKET. You are also the standing substitute when a
worker stalls.

## Standing duties (all phases)

- **Independently re-run every test before accepting any TASK-DONE.** A described pass is a
  failure.
- **Red-first verification**: each sentinel carries `red-first-Y/N`; spot-check by stubbing the
  implementation out and confirming the test actually fails.
- Confirm by ARTIFACTS: exit codes, `git status` deltas, `duckdb` row counts, file mtimes —
  never spinner text, never a notification alone.
- Paste raw command output into the board for every check you run.

## GATE checklist (ALL must pass; raw output pasted)

1. `just check` — ruff check + ruff format --check + pyrefly + codespell + full pytest, zero
   warnings.
2. `uv run pytest -q` — every suite green: store, ingest×3 (otlp, jsonl, langsmith), web, jobs,
   e2e; red-first counts per pane recorded.
3. **OTLP round-trip**: `curl -sf -X POST localhost:4318/v1/logs -d
   @tests/fixtures/otlp/api_request.json -H 'Content-Type: application/json'` then reload `/` —
   fixture event visible.
4. **Three-source count**: `duckdb "$(uv run bam config db-path)" "SELECT source, count(*) FROM
   events GROUP BY 1"` — `otlp`, `jsonl`, and `langsmith` all > 0. ALWAYS the resolved-path form:
   a bare `$BAM_DB_PATH` is unset in your shell and silently opens an empty in-memory DB, making
   this check pass/fail meaninglessly.
5. **LangSmith read-back cross-check**: `langsmith run list --project "$LANGSMITH_PROJECT"
   --limit 10` pasted next to `duckdb "$(uv run bam config db-path)" "SELECT count(*) FROM events
   WHERE source='langsmith'"` — LangSmith's runs and our ingested rows must be consistent
   (unmatched runs allowed only in the visible LangSmith-only bucket). PREFLIGHT already asserted
   `$CC_LANGSMITH_PROJECT` (what the poller ingests) equals `$LANGSMITH_PROJECT` (what you
   query), so this comparison is apples-to-apples.
6. **Docker round-trip**: `docker compose up --build -d && curl -sf localhost:8000/ && curl -sf
   -X POST localhost:4318/v1/logs -H 'Content-Type: application/json' -d
   @tests/fixtures/otlp/api_request.json` — then `docker compose down`.
7. `uvx marimo run notebooks/explore.py` — opens read-only against a live DB without errors.
8. Agent-loop artifacts: `docs/AGENT_LOOP.md` + before/after screenshots under
   `docs/img/agent-loop/`, referenced from the doc.

> RUN NOTE: the spec's Validation list also includes `gh run watch --exit-status` (CI green).
> This run does NOT push — CI-on-GitHub is the recorded DEFERRED item, not a GATE criterion.
> Never fake or infer a CI-green claim.

## Live-telemetry generation recipe (feeds checks 3–5)

With the app running, launch ONE headless session — this exact hook-safe inline command (do NOT
try to source the env sample file: any Bash command containing its name is hook-blocked):

```
CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_PROTOCOL=http/json OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
claude -p "say hi"
```

One command covers all three sources: it emits real OTLP events AND writes a real
`~/.claude/projects` JSONL transcript; the LangSmith tracing plugin (`TRACE_TO_LANGSMITH`,
ambient via direnv) covers the third source on the poller's next cycle (default 60s — wait for
it, then check). Also verify on the dashboard: live events on `/live` within seconds; the
session page shows a per-task timeline with cost.

## Secrets discipline

LangSmith auth is ambient (direnv). Never `cat` env files, never echo key values — presence
checks only (`test -n "$LANGSMITH_PROJECT"`). If the `langsmith` CLI suddenly loses auth, the
envrc was probably edited and re-blocked: fix is bare `direnv allow` (no path argument — the
path contains a hook-blocked token).
