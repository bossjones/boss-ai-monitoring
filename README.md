# boss-ai-monitoring

A single-user, single-host observability dashboard for [Claude Code](https://claude.com/claude-code)
usage: cost, tokens by class, tool success rates, session timelines, and quality signals — pulled
from three sources into one DuckDB file and served as a fast, no-build-step web UI.

> **Status: pre-build.** This repo currently contains the spec and the build prompts; the
> application code is produced by a multi-agent build run (see [How this gets built](#how-this-gets-built)).
> Sections marked *(filled in by the build)* are extended in place by that run.

## What it does

- **Ingests three signal sources** into a canonical `events` table:
  1. **OTLP telemetry** — Claude Code's native OpenTelemetry export (http/json, port 4318,
     received directly by the app; no collector).
  2. **JSONL transcripts** — incremental reads of `~/.claude/projects/**/*.jsonl` (mounted
     read-only), for backfill and gap-fill. JSONL-derived costs are flagged estimates.
  3. **LangSmith runs** — cursor-based polling of the LangSmith API (rate-limit aware; degrades
     gracefully without an API key).
- **Stores everything in one DuckDB file** — canonical event envelope with a JSON `payload`
  column, batched idempotent writer, single write connection.
- **Serves a dashboard** (FastAPI + Jinja2 + htmx + SSE): `/` overview with stat tiles and a
  14-day cost sparkline, `/live` streaming feed, `/sessions/{id}` per-task timelines with
  LangSmith deep-links, `/costs` rollups and attribution. Every panel has a JSON twin and a
  provenance/freshness footer.
- **Runs trailing quality jobs**: correction-language scan, OTel-vs-JSONL drift self-check, error
  classification. Deterministic heuristics only — no LLM-judge in v1.
- **Ships a marimo notebook** (`notebooks/explore.py`) for ad-hoc exploration over a read-only
  DuckDB connection, and a Docker Compose deployment (ports 8000 + 4318).

The full design — phases, schemas, routes, edge cases, and every decided question — lives in the
spec: [`specs/boss-ai-monitoring/boss-ai-monitoring.html`](specs/boss-ai-monitoring/boss-ai-monitoring.html).

## Quickstart *(filled in by the build)*

```bash
uv run bam serve   # then open http://localhost:8000
```

Point Claude Code's telemetry at the receiver (see `.env.sample` for the full variable set):

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

## Architecture *(filled in by the build)*

```
Claude Code ──OTLP http/json──▶ :4318 ┐
~/.claude/projects/**/*.jsonl ──read-only poll──▶ ├─▶ DuckDB (events) ──▶ FastAPI + htmx dashboard :8000
LangSmith API ──cursor poll──▶        ┘                        └─▶ trailing quality jobs
```

## LangSmith setup *(filled in by the build)*

Tracing in (Claude Code → LangSmith) uses the LangSmith Claude Code plugin (`TRACE_TO_LANGSMITH`,
`CC_LANGSMITH_API_KEY`, `CC_LANGSMITH_PROJECT`); polling out (LangSmith → dashboard) uses
`LANGSMITH_API_KEY`. The [`langsmith` CLI](https://github.com/langchain-ai/langsmith-cli) provides
read-back verification: `langsmith run list --project "$LANGSMITH_PROJECT"`.

## Privacy

Content-capture OTel flags (`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`,
`OTEL_LOG_TOOL_DETAILS`) stay **off** by default — telemetry is metadata unless you opt in.
Dashboard and OTLP ports bind localhost; single-user, trusted-network scope; no auth by design.

## How this gets built

The implementation is executed by a 7-pane multi-agent cmux team driven by
[`prompts/boss-ai-monitoring-build-team.md`](prompts/boss-ai-monitoring-build-team.md) — TDD
red-first, exclusive file ownership per pane, `just check` as the definition of done, local
commits only (the human pushes after review). See [`prompts/README.md`](prompts/README.md) for the
prompt lineage and [`CLAUDE.md`](CLAUDE.md) for the conventions any Claude Code session in this
repo must follow.
