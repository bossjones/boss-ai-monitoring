# shared.md — read by EVERY pane before its own brief

> Derived from `../boss-ai-monitoring.html` (canonical) on 2026-07-11. If this conflicts with the
> HTML or with observed behavior, the evidence wins — file an OQ. Read-only during the run.

## What we're building

A locally-run, KISS observability tool for Claude Code that answers in one place, near-realtime:
*What are my agents doing right now? How long does each task take? What does each session, agent,
skill, and model cost? Which tools succeed and fail? Am I getting better or worse over time?*

## Architecture

```
Claude Code ──OTLP http/json──▶ FastAPI :4318  /v1/logs + /v1/metrics   (live, authoritative cost)
~/.claude/projects/**/*.jsonl ─▶ incremental JSONL reader (byte-offset cursors; history + gap-fill)
LangSmith cloud ◀──hooks plugin traces… ──▶ poller: Client.list_runs(start_time cursor)
                                   │
                                   ▼  batched writes (Appender)
                        ┌─────────────────────────┐
                        │  one DuckDB file        │  raw `events` (JSON payload col)
                        │  + SQL views = metrics  │  lineage: otlp | jsonl | langsmith
                        └─────────────────────────┘
                                   │
                                   ▼
        FastAPI + Jinja2 + htmx + SSE dashboard  (live feed, per-task timeline, cost rollups)
        [optional] marimo notebook on the same DuckDB file (ad-hoc exploration)
```

Design principles from the spec's Solution section:

- **Events over metrics** — OTel log events are the primary stream; the 8 official metrics and
  the "5 metrics that matter" are derived SQL views. `prompt.id` groups everything downstream of
  one user prompt = per-task timing. `session.id` ↔ LangSmith `thread_id` = merge key.
- **Anthropic telemetry discipline** — every event row carries `model`, `git_sha`,
  `agent_name`/`skill_name`, and `source` lineage so regression time-series and per-feature
  cost/latency attribution work in plain SQL ("store results like telemetry, not test logs").
- **Watcher discipline** — anything analytical runs as *trailing background jobs* over stored
  data, never on the agent's critical path.
- **Agent-editable frontend** — server-rendered HTML/htmx is the format a coding agent edits most
  reliably; Phase 7 codifies the screenshot → critique → edit → reload loop.

## The canonical event envelope (everyone writes/reads this)

`events` table columns: `event_id` (pk), `ts`, `source` (otlp|jsonl|langsmith), `event_type`,
`session_id`, `prompt_id`, `request_id` (nullable — the API request identifier: OTel attribute
on `api_request` events / JSONL `requestId`; keys the JSONL↔OTel dedupe), `model`, `git_sha`,
`agent_name`, `skill_name`, `tool_name`, `cost_usd`, `duration_ms`, `tokens_input`,
`tokens_output`, `tokens_cache_read`, `tokens_cache_creation`, `success`, `cwd`, `payload` (JSON).

`ingest_cursors` table: `source`, `key`, `cursor`, `updated_at`.

Unknown/extra fields always land in `payload` — views never depend on exhaustive parsing, so new
event types need no migrations. All writes go through the single batched writer owned by
`store/writer.py` (one write connection, period). Everything else opens read-only connections.

## Ground truths (do not re-litigate; full list in the team prompt G1–G14)

- OTLP is **http/json only** on **:4318**; dashboard on **:8000**; no gRPC, no otel-collector.
- Type checker = **pyrefly** (use the `/agent-harness:pyrefly-typing` skill for annotations).
- Config precedence: env (`BAM_` prefix, `__` nesting) > YAML (`config.yaml`/`$BAM_CONFIG`) >
  defaults. **No `os.environ` reads outside `config.py`.**
- JSONL-derived costs are ESTIMATES — flagged, excluded from cost views when an OTel cost exists
  for the same `session_id + request_id`.
- Content-capture OTel flags (`OTEL_LOG_USER_PROMPTS`/`_ASSISTANT_RESPONSES`/`_TOOL_DETAILS`)
  stay OFF by default. Localhost bind, no auth, single-user scope.
- htmx vendored as one static file — no npm, no build step.
- v1 quality scoring is deterministic/heuristic — no LLM-judge.
- Commit locally at phase boundaries; **never push**; `just check` is the definition of done.
- Exclusive file ownership, ONE exception: `.team/*.backlog.md` and `.team/*.open-questions.md`
  are shared APPEND-ONLY (any pane appends an entry; only the lead edits/triages existing ones).
- Serving model: ONE FastAPI app (otlp router mounted into `web/app.py`), TWO uvicorn binds —
  `bam serve` (lead's `cli.py`) serves the same app on :8000 and :4318.
- The resolved DuckDB path comes from `uv run bam config db-path` — never rely on a bare
  `$BAM_DB_PATH` in shell commands (it's unset in pane shells and silently opens an empty DB).

> RUN NOTE: test layout is `tests/unit/**`, `tests/integration/**`, `tests/e2e/**` (prompt G13),
> not the spec's literal `tests/store` / `tests/ingest/...` paths. Where this brief set quotes an
> acceptance command, the path is already translated.

## Claude Code OTel quick reference (spec Notes, verbatim)

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# optional: OTEL_METRIC_EXPORT_INTERVAL=60000, OTEL_LOGS_EXPORT_INTERVAL=5000
```

- **Metrics (8):** `session.count`, `lines_of_code.count`, `pull_request.count`, `commit.count`,
  `cost.usage`, `token.usage`, `code_edit_tool.decision`, `active_time.total`.
- **Key log events:** `api_request` (cost_usd, duration_ms, 4 token classes), `tool_result`
  (tool_name, success, duration_ms, error_type), `tool_decision` (accept/reject + source),
  `user_prompt`, `compaction`, `api_error`. Every event carries `session.id` + `prompt.id`.
- **Gotchas:** metrics use delta temporality (that's why views prefer events); `OTEL_*` vars are
  NOT inherited by Bash/hooks/MCP subprocesses. OTel logs export every ~5s — realtime enough for
  the live feed (hooks-based push is deferred, not MVP).
- Docs: code.claude.com/docs/en/monitoring-usage

## LangSmith reference (spec Notes, verbatim + run additions)

- **Tracing in** (Claude Code → LangSmith): `/plugin marketplace add
  langchain-ai/langsmith-claude-code-plugins`, then `TRACE_TO_LANGSMITH=true`,
  `CC_LANGSMITH_API_KEY`, `CC_LANGSMITH_PROJECT`. Hooks-based, not OTel; client-side secret
  redaction on by default; coexists with the OTel pipeline.
- **Pulling out** (LangSmith → our store): `langsmith.Client().list_runs(project_name=…,
  start_time=…, select=[…], filter=…)`. Rate limits ~10 req/10s on ≤7-day windows → cursor
  polling, never full scans.
- **Two-key reality:** `CC_LANGSMITH_API_KEY` feeds the plugin; `LANGSMITH_API_KEY` feeds the
  poller and the `langsmith` CLI. Both arrive ambiently via direnv in every pane — never print
  them, never read the envrc file (prompt G14).
- **Read-back CLI** (preinstalled, v0.2.39): `langsmith run list --project
  "$LANGSMITH_PROJECT"`, `langsmith trace get`, `langsmith thread list` — use it to verify what
  LangSmith actually holds vs what we ingested.

## Metric definitions the dashboard commits to (spec Notes, verbatim)

- **Per-task time** — wall-clock between first and last event sharing a `prompt.id`; user-vs-CLI
  split from `active_time.total`.
- **The 5 metrics that matter:** Task Completion Rate; Tool Selection Accuracy
  (`tool_result.success`); Autonomy Score (`tool_decision.source` config vs user); Recovery Rate
  (error→retry patterns); Cost per Successful Task.
- **Anthropic-style quality telemetry:** every row carries model + git SHA + agent/skill attrs so
  pass/regression queries are time-series; correction-language frequency as a production quality
  signal; drift sanity-checks against a blessed source; provenance footers for trust. Their
  measured tradeoff (adversarial-review subagents: +6% accuracy, +32% tokens, +72% latency) is
  the archetype for the `/costs` attribution view.

## Dependencies (all via `uv add`)

Runtime: `fastapi uvicorn duckdb jinja2 httpx langsmith pydantic pydantic-settings pyyaml
sse-starlette`. Dev: `pytest pytest-asyncio respx ruff pyrefly codespell pre-commit playwright`.
Notebooks group: `marimo`.

## The two named RISKs (file OQs against these early, that's a SUCCESS)

1. **JSONL format is reverse-engineered** and shifts across Claude Code versions. Mitigations:
   unknown fields → `payload`; malformed lines skip-and-log; Phase 8 drift self-check catches
   systematic divergence.
2. **LangSmith `thread_id` ↔ `session.id` join is best-effort.** Mitigations: validated in Phase
   5 E2E against a real traced session; unmatched runs stay visible in a LangSmith-only bucket
   with timestamp-proximity as a secondary hint, never silently merged.
