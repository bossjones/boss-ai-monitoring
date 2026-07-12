# 1. First run: from zero to your first event

## What you'll do

Install dependencies, start `bam serve`, point a Claude Code session's telemetry at it, and watch
your first events land on `/live`. ~10 minutes.

## Prerequisites

- Python 3.13 and [`uv`](https://docs.astral.sh/uv/) installed.
- `boss-ai-monitoring` cloned locally (this repo).
- Claude Code installed, so you have something to generate telemetry with.
- Nothing else. `LANGSMITH_API_KEY` is optional — the LangSmith poller degrades gracefully
  without it (see step 6).

## What you'll end up with

A dashboard at `http://localhost:8000` showing cost, tokens, and tool stats, and a `/live` page
streaming events from a Claude Code session as they arrive.

## Step 1 — install dependencies

```bash
uv sync
```

`uv sync` resolves and installs into `.venv/`. Expected output ends with something like:

```
Resolved 75 packages in 0.55ms
Installed N packages in ...
```

(If you already ran `uv sync` before, you may see "Audited N packages" instead — that's fine, it
means nothing changed.)

> The marimo notebook dependency (used in [tutorial 2](02-exploring-your-data.md)) lives in a
> separate `notebooks` dependency group and is **not** installed by plain `uv sync`. You don't
> need it for this tutorial — `uvx marimo ...` fetches it standalone when you get there.

## Step 2 — start the app

```bash
uv run bam serve
```

`bam serve` is the one entrypoint: it runs **one** FastAPI app on **two** binds in a single
asyncio loop — the dashboard on `:8000` and the OTLP receiver on `:4318`. There is no separate
collector process and no gRPC; the app speaks OTLP http/json directly. Expected output:

```
INFO bam: serving dashboard on 127.0.0.1:8000 and OTLP on 127.0.0.1:4318
INFO:     Started server process [...]
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Uvicorn running on http://127.0.0.1:4318
```

Leave this running in its own terminal for the rest of the tutorial.

For iterative dev work later, `just dev` runs the same command with reload enabled
(`uv run bam serve` under the hood).

## Step 3 — open the dashboard

Open [http://localhost:8000](http://localhost:8000) in a browser. On a completely fresh install
(no events ingested yet), the overview page renders its empty state rather than erroring — the
DuckDB file doesn't exist until the first event is written, and every panel is built to degrade
gracefully when it's missing.

## Step 4 — point a Claude Code session at the receiver

In a **separate terminal** — the one where you'll actually run Claude Code — export the telemetry
variables (also documented in [`.env.sample`](../../.env.sample)):

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Notes:

- `OTEL_EXPORTER_OTLP_PROTOCOL` must be `http/json` — the receiver does not speak gRPC.
- These four variables only affect telemetry export; they don't touch anything else about how
  Claude Code runs.
- Privacy: content-capture flags (`OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`,
  `OTEL_LOG_TOOL_DETAILS`) are commented out in `.env.sample` and stay off by default — telemetry
  is metadata (costs, tokens, durations, tool names) unless you deliberately opt in.

## Step 5 — use Claude Code, then watch `/live`

With those variables exported, start a Claude Code session in that terminal and do a bit of normal
work — a prompt or two, a tool call. Then open
[http://localhost:8000/live](http://localhost:8000/live) and refresh (or just watch — `/live`
streams over SSE).

What you should see:

- Individual events (`api_request`, `tool_result`, etc.) appearing in the event feed, each tagged
  with `source: otlp`.
- An active-session card once events for your session start arriving, showing running cost and
  duration.

There's an export interval on the Claude Code side (tunable via `OTEL_LOGS_EXPORT_INTERVAL`, see
`.env.sample`), so don't expect literal real-time — a few seconds of lag between an action in
Claude Code and its row showing up on `/live` is normal.

## What each page shows

- **`/`  (overview)** — today's cost, token totals by class (input/output/cache-read/cache-creation),
  active session count, tool success rate, a 14-day cost sparkline, and a short list of recent
  sessions. Every panel has a JSON twin at `/api/overview`.
- **`/live`** — the raw event feed (via SSE) plus cards for currently-active sessions. This is the
  page to have open while you're actively using Claude Code.
- **`/sessions/{id}`** — a per-task timeline for one session: each task's duration, cost, token
  counts, tool call success/failure counts, and (when a match exists) a deep-link to the
  corresponding LangSmith trace.
- **`/costs`** — daily/weekly cost rollups, attribution by model/agent/skill, and the "5 metrics
  that matter" summary (task completion rate, tool selection accuracy, autonomy score, recovery
  rate, cost per successful task).

Every panel also carries a **provenance/freshness footer**: per-source (`otlp` / `jsonl` /
`langsmith`) last-seen timestamp and event count, so you always know which source contributed a
given number and how fresh it is.

## Step 6 — where the other two sources fit in

You don't need to do anything for these — they're automatic:

- **JSONL transcripts**: `~/.claude/projects/**/*.jsonl` (mounted/read read-only) are scanned on
  an interval (`ingest.jsonl_scan_interval_s`, default 15s) and used to backfill history — past
  sessions you had before `bam serve` was running, and gap-fill for whatever happened while the
  OTLP endpoint was down. Rows from this source are tagged `source: jsonl`; JSONL-derived costs
  are flagged as **estimates** and are excluded from cost views whenever an OTel-derived cost
  exists for the same `(session_id, request_id)` — OTel is the authoritative cost source.
- **LangSmith runs**: if `LANGSMITH_API_KEY` is set, a cursor-based poller pulls runs from the
  LangSmith API on an interval (`ingest.langsmith_poll_interval_s`, default 60s) and tags them
  `source: langsmith`. Without an API key, this poller degrades gracefully — the app still boots
  and serves; you just won't see `source: langsmith` rows.

Check what's landed at any time, per source:

```bash
duckdb "$(uv run bam config db-path)" "SELECT source, count(*) FROM events GROUP BY 1"
```

(Only works while the app is idle — see [tutorial 2](02-exploring-your-data.md) for why, and what
to do once it's running and ingesting.)

## Next steps

- [Tutorial 2: exploring your data](02-exploring-your-data.md) — ad-hoc SQL and marimo, and the
  DuckDB locking gotcha you need to know about before you query the live database.
- [Tutorial 3: running in Docker](03-running-in-docker.md).
- [Tutorial 4: troubleshooting](04-troubleshooting.md) if events aren't showing up.
