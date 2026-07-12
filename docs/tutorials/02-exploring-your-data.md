# 2. Exploring your data: ad-hoc querying

## What you'll do

Run ad-hoc SQL against your DuckDB file — with the app stopped and, more usefully, **while it's
running** — and open the marimo notebook for interactive exploration. ~10 minutes.

## Prerequisites

- Completed [tutorial 1](01-first-run.md): `bam serve` has ingested at least a few events, so
  `$(uv run bam config db-path)` points at a real file.
- `duckdb` CLI installed (`brew install duckdb` or see the [DuckDB docs](https://duckdb.org/docs/installation/)).

## The one thing you need to know first

**DuckDB's file lock is exclusive, cross-process.** Once `bam serve` has written anything, a
second process — the `duckdb` CLI, a marimo notebook, anything — cannot open that same file at
all, not even read-only:

```bash
$ duckdb "$(uv run bam config db-path)" "SELECT source, count(*) FROM events GROUP BY 1"
IO Error: Could not set lock on file ".../bam.duckdb": Conflicting lock is held in ... (PID ...)
```

This is a DuckDB property (`-readonly` does **not** get around it), not a bug in this app.

**The trap:** the write connection is created *lazily* — only when the writer actually needs to
flush a batch. So immediately after starting `bam serve`, before any event has been ingested, the
app is holding no lock yet, and a direct query against the live file will appear to work. The
moment the first event lands, that direct-query command starts failing. Don't rely on "it worked
a minute ago" — check the actual state.

**The fix is never to stop the app.** Use `bam snapshot`:

```bash
duckdb "$(uv run bam snapshot)" "SELECT source, count(*) FROM events GROUP BY 1"
```

## Step 1 — with the app stopped: query directly

If `bam serve` is not running, you can open the live file directly — no trick needed:

```bash
duckdb "$(uv run bam config db-path)" "SELECT source, count(*) FROM events GROUP BY 1"
```

Expected output looks like:

```
┌───────────┬──────────────┐
│  source   │ count_star() │
│  varchar  │    int64     │
├───────────┼──────────────┤
│ otlp      │          203 │
│ langsmith │           50 │
│ jsonl     │       107590 │
└───────────┴──────────────┘
```

## Step 2 — start the app, then hit the lock

Start the app back up in another terminal:

```bash
uv run bam serve
```

Use Claude Code for a moment so at least one event actually gets written (the lazy-writer trap
above — an idle app holds no lock), then try the same direct query again:

```bash
duckdb "$(uv run bam config db-path)" "SELECT 1"
```

You should now get the lock error shown above. This is expected — move on to `bam snapshot`.

## Step 3 — query a snapshot instead

```bash
duckdb "$(uv run bam snapshot)" "SELECT source, count(*) FROM events GROUP BY 1"
```

`bam snapshot` asks the **running app** for a consistent, point-in-time copy of the database —
tables *and* views — and prints only the path to stdout, which is why it composes cleanly inside
`$(...)`. Under the hood it's a `POST /api/snapshot` call against the running dashboard; if
nothing is listening on that port, it falls back to snapshotting in-process, so the same command
works whether the app is up or down. Snapshot files land in `store.snapshot_dir` (defaults to
`<db_path's parent>/snapshots`, so it follows wherever your DB actually lives — e.g. the Docker
volume in containerized deployments).

## Useful queries

All of these work against a snapshot (or the live file, when the app is stopped). Swap
`"$(uv run bam snapshot)"` for `"$(uv run bam config db-path)"` if the app is idle.

**Row counts per source** — sanity-check that all three ingest paths are contributing:

```bash
duckdb "$(uv run bam snapshot)" "SELECT source, count(*) FROM events GROUP BY 1"
```

**Daily cost** (`v_costs_daily` — already excludes JSONL-estimate rows wherever an OTel cost
exists for the same request, per the app's cost-dedup rule):

```bash
duckdb "$(uv run bam snapshot)" "SELECT * FROM v_costs_daily ORDER BY day DESC LIMIT 14"
```

```
┌────────────┬──────────┬─────────────┐
│    day     │ cost_usd │ event_count │
│    date    │  double  │    int64    │
├────────────┼──────────┼─────────────┤
│ 2026-07-12 │ 1.063884 │           2 │
│ 2026-07-11 │ 0.632105 │           5 │
└────────────┴──────────┴─────────────┘
```

**Recent sessions, cost and model** (`v_sessions`):

```bash
duckdb "$(uv run bam snapshot)" \
  "SELECT session_id, started_at, cost_usd, model FROM v_sessions ORDER BY started_at DESC LIMIT 10"
```

**Tools with failures** (`v_tool_stats` — success rate and p50/p95 duration per tool):

```bash
duckdb "$(uv run bam snapshot)" \
  "SELECT tool_name, call_count, success_count, success_rate FROM v_tool_stats WHERE success_rate < 1.0 ORDER BY call_count DESC"
```

**Token mix across everything ingested:**

```bash
duckdb "$(uv run bam snapshot)" \
  "SELECT sum(tokens_input) AS tokens_input, sum(tokens_output) AS tokens_output, sum(tokens_cache_read) AS tokens_cache_read, sum(tokens_cache_creation) AS tokens_cache_creation FROM events"
```

**One specific session's raw events** (useful when a dashboard number looks wrong and you want to
see exactly what rows produced it):

```bash
duckdb "$(uv run bam snapshot)" \
  "SELECT ts, source, event_type, tool_name, cost_usd, success FROM events WHERE session_id = '<session-id>' ORDER BY ts"
```

Other views worth knowing about: `v_tasks` (per-prompt rollup — one user prompt = one task),
`v_attribution` (cost + event count by agent/skill/model), `v_five_metrics` (a single summary row:
task completion rate, tool selection accuracy, autonomy score, recovery rate, cost per successful
task), and `v_cost_events` (the raw JSONL-vs-OTel dedup that every cost view is built on, if you
want to see exactly which rows got excluded as duplicate estimates).

## Step 4 — interactive exploration with marimo

The repo ships a notebook at `notebooks/explore.py` with drill-downs already built (daily cost →
day picker → that day's sessions, token mix, tool failure explorer). It only ever opens a
**read-only** connection — same locking rule applies.

App stopped:

```bash
uvx marimo edit notebooks/explore.py
```

App running — point the notebook at a snapshot instead, via the `BAM_STORE__DB_PATH` env var:

```bash
BAM_STORE__DB_PATH="$(uv run bam snapshot)" uvx marimo edit notebooks/explore.py
```

> **Note the env var name carefully: `BAM_STORE__DB_PATH`** — `BAM_` prefix, double underscore,
> then the nested config key (`store.db_path`). It is **not** `BAM_DB_PATH`. (`BAM_DB_PATH` does
> exist as a legacy flat alias for the same setting, but the nested spelling is canonical — use
> it. See [tutorial 4](04-troubleshooting.md) for what happens if you get this wrong.)

`uvx marimo edit` opens an interactive, editable session in your browser; `uvx marimo run
notebooks/explore.py` runs it as a read-only app view instead, if that's all you want.

## Next steps

- [Tutorial 3: running in Docker](03-running-in-docker.md).
- [Tutorial 4: troubleshooting](04-troubleshooting.md) — the full symptom → cause → fix reference,
  including the DuckDB lock error and the `BAM_DB_PATH` footgun in more detail.
