# boss-ai-monitoring

A single-user, single-host observability dashboard for [Claude Code](https://claude.com/claude-code)
usage: cost, tokens by class, tool success rates, session timelines, and quality signals — pulled
from three sources into one DuckDB file and served as a fast, no-build-step web UI.

> **Status: built.** The application code was produced by the multi-agent build run described in
> [How this gets built](#how-this-gets-built). The full design — phases, schemas, routes, edge
> cases, and every decided question — remains in the spec:
> [`specs/boss-ai-monitoring/boss-ai-monitoring.html`](specs/boss-ai-monitoring/boss-ai-monitoring.html).

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

## Quickstart

```bash
uv sync                     # Python 3.13, src/ layout, hatchling
uv run bam serve            # then open http://localhost:8000
```

`bam serve` runs **one** FastAPI app on **two** binds in a single asyncio loop — the dashboard on
`:8000` and the OTLP receiver on `:4318`. There is no collector and no gRPC; the app speaks OTLP
http/json directly.

Point Claude Code's telemetry at the receiver (see the sample env file for the full variable set):

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Then use Claude Code as usual and watch `/live`. Useful commands:

```bash
just check                              # the definition of done: ruff + format + pyrefly + codespell + pytest
just dev                                # bam serve with reload
uv run bam config db-path               # print the RESOLVED DuckDB path
duckdb "$(uv run bam config db-path)" "SELECT source, count(*) FROM events GROUP BY 1"
```

> Always use `"$(uv run bam config db-path)"` in shell commands. A bare `$BAM_DB_PATH` is unset in
> most shells and silently opens an *empty* database — which looks like a clean result and is a lie.

## Configuration

Precedence: **environment variables > `config.yaml` > code defaults.** Env vars use the `BAM_`
prefix and `__` for nesting (`BAM_SERVER__DASHBOARD_PORT=9000`). Copy
[`config.sample.yaml`](config.sample.yaml) to `config.yaml`, or point `$BAM_CONFIG` at a file
elsewhere. A missing config file is fine — defaults apply. A *malformed* one is a startup error
that names the file and line, because a silently-ignored config is worse than a crash.

All settings flow through `config.py`; nothing else in the codebase reads the environment.

## Architecture

```
Claude Code ──OTLP http/json──▶ :4318 ┐
~/.claude/projects/**/*.jsonl ──read-only poll──▶ ├─▶ DuckDB (events) ──▶ FastAPI + htmx dashboard :8000
LangSmith API ──cursor poll──▶        ┘                        └─▶ trailing quality jobs
```

Three ingest sources land in one canonical `events` table with a JSON `payload` column, so new
event types need no migrations. Every row carries its `source` lineage (`otlp` | `jsonl` |
`langsmith`), so you always know where a number came from — that is what the provenance footer on
each panel reports. Metrics are SQL **views** over those events, not a separate pipeline.

Writes go through a single batched, idempotent writer behind **one** DuckDB write connection;
everything else (dashboard, jobs, notebook) reads. OTel is the authoritative cost source: JSONL
costs are flagged **estimates** and are excluded from cost views whenever an OTel cost exists for
the same `(session_id, request_id)`.

## LangSmith setup

Two keys, two jobs. Tracing *in* (Claude Code → LangSmith) uses the LangSmith Claude Code plugin
(`TRACE_TO_LANGSMITH`, `CC_LANGSMITH_API_KEY`, `CC_LANGSMITH_PROJECT`). Polling *out*
(LangSmith → dashboard) uses `LANGSMITH_API_KEY`. The poller is cursor-based and rate-limit aware,
and degrades gracefully when no API key is configured — the app still boots and serves.

`CC_LANGSMITH_PROJECT` and `LANGSMITH_PROJECT` must name the **same** project, or you will trace
into one and read back from another. The
[`langsmith` CLI](https://github.com/langchain-ai/langsmith-cli) is the read-back check:

```bash
langsmith run list --project "$LANGSMITH_PROJECT" --limit 10
```

The `session.id` ↔ LangSmith `thread_id` join is **best-effort** by design. Runs that don't match
stay visible in a LangSmith-only bucket rather than being silently merged into the wrong session.

## Exploring the data (marimo)

```bash
uvx marimo edit notebooks/explore.py    # interactive
uvx marimo run notebooks/explore.py     # app mode
```

The notebook opens a **read-only** DuckDB connection, so it never fights the running app's writer.

## Running in Docker

```bash
docker compose up --build      # dashboard :8000, OTLP :4318
```

One service, a volume for the DuckDB file, and a **read-only** mount of `~/.claude/projects`.

> **macOS gotcha:** a Claude Code session on the *host* cannot reach the container at
> `localhost:4318`. Point it at `host.docker.internal:4318` instead
> (`OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318`). This is the most common reason
> "no events arrive" when running containerized.

## Known limitations

**You cannot query the DuckDB file from another process while the app is running.** DuckDB takes an
exclusive file lock, so `duckdb "$(uv run bam config db-path)" ...` and `marimo run
notebooks/explore.py` both fail with `IO Error: Could not set lock on file ...` while `bam serve`
holds it. `duckdb -readonly` does **not** get around this — the lock is exclusive regardless.

Stop the app first, then run your query or open the notebook:

```bash
# stop `bam serve`, then:
duckdb "$(uv run bam config db-path)" "SELECT source, count(*) FROM events GROUP BY 1"
```

This is inherent to the single-file, single-writer design (one DuckDB file, one write connection) —
the tradeoff that buys the app its simplicity. In-process readers are unaffected: the dashboard and
the trailing jobs read happily while ingest is writing.

## Privacy

Content-capture OTel flags (`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`,
`OTEL_LOG_TOOL_DETAILS`) stay **off** by default — telemetry is metadata unless you opt in.
Dashboard and OTLP ports bind localhost; single-user, trusted-network scope; no auth by design.
`~/.claude/projects` is mounted read-only everywhere it is read — locally and in the container.

## How this gets built

The implementation is executed by a 7-pane multi-agent cmux team driven by
[`prompts/boss-ai-monitoring-build-team.md`](prompts/boss-ai-monitoring-build-team.md) — TDD
red-first, exclusive file ownership per pane, `just check` as the definition of done, local
commits only (the human pushes after review). See [`prompts/README.md`](prompts/README.md) for the
prompt lineage and [`CLAUDE.md`](CLAUDE.md) for the conventions any Claude Code session in this
repo must follow.
