# API Reference

All routes are served by the single `FastAPI` app object built in
`src/boss_ai_monitoring/web/app.py::create_app`. Dashboard-facing routes (`/`, `/live`,
`/sessions/{id}`, `/costs`, their `/api/*` JSON twins, `/api/events/stream`, `/api/snapshot`) live
in `web/app.py`. The OTLP ingest routes (`/v1/logs`, `/v1/metrics`) live in `ingest/otlp.py` and are
mounted into the same app object under the `# otlp-mount` marker in `create_app`. Because it is one
app on two binds (see `docs/ARCHITECTURE.md`), every route technically answers on both
`dashboard_bind:dashboard_port` (default `127.0.0.1:8000`) and `otlp_bind:otlp_port` (default
`127.0.0.1:4318`) — in practice, Claude Code's OTel exporter is pointed at `:4318` and a browser at
`:8000`.

No authentication anywhere. Both binds default to `127.0.0.1`. This is a stated design constraint,
not an oversight — see `docs/ARCHITECTURE.md` and the `POST /api/snapshot` entry below.

## OTLP ingest

### `POST /v1/logs`

Accepts an OTLP `ExportLogsServiceRequest`, JSON-encoded (`resourceLogs -> scopeLogs ->
logRecords`). Parsed by `ingest/otlp.py::parse_logs_payload`.

- **Request body**: JSON object matching the OTLP JSON log export shape. `Content-Encoding: gzip`
  is accepted and transparently decompressed. Any missing or unexpected substructure
  (`resourceLogs`, `scopeLogs`, `logRecords`, `attributes`) yields fewer parsed events, never an
  error — the parser walks with defensive `.get(..., [])` throughout.
- Each log record's `event.name` attribute becomes `event_type`; if absent, `event_type` is the
  literal string `"unknown"` — the record is still stored, not dropped.
- A fixed set of attribute keys (`ingest/otlp.py::_ATTR_TO_COLUMN` — `session.id`, `prompt.id`,
  `request_id`, `model`, `git_sha`/`git.sha`, `agent.name`, `skill.name`, `tool_name`, `cost_usd`,
  `duration_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`,
  `success`, `cwd`) map onto the canonical `events` columns. Every other attribute — including from
  event types the mapping table has never seen — is stored verbatim in the row's `payload` JSON
  column rather than rejected.
- `event_id` is `sha256(source|session_id|timeUnixNano|json(record))` — deterministic on the
  record's own content, so replaying an export batch is a no-op against the writer's
  anti-join-on-`event_id` dedupe, not a duplicate insert.
- Events are written and flushed synchronously before the response returns
  (`_write_and_flush` calls `writer.write_many(...)` then `writer.flush()`).

| Status | Condition |
|---|---|
| `200` | Body `{}`. Events written (possibly zero, if the payload had no records). |
| `400` | Body is not valid JSON (`invalid JSON body: ...`), the gzip stream is invalid (`invalid gzip body`), or the decoded body is not a JSON object (`ExportLogsServiceRequest must be a JSON object`). |
| `413` | `Content-Length` declares a body larger than `ingest.otlp_max_body_bytes` (checked before the body is read), **or** the actual (post-decompression) body exceeds it (checked after decompression, so a gzip bomb is still caught). Default limit: 10,485,760 bytes (10 MiB). |

### `POST /v1/metrics`

Same contract as `/v1/logs`, for an OTLP `ExportMetricsServiceRequest`
(`resourceMetrics -> scopeMetrics -> metrics -> {sum,gauge,histogram,summary}.dataPoints`), parsed
by `ingest/otlp.py::parse_metrics_payload`. One `events` row is written per data point, with
`event_type = "metric"` (the literal marker — the actual OTel metric name is queryable via
`payload->>'metric_name'`) and `payload.metric_name` / `payload.unit` / `payload.value` populated.
Same status codes, same gzip/size handling, same "unknown attributes go to `payload`, never
rejected" behavior.

## Dashboard (HTML + JSON twins)

Every HTML route renders the full page template normally, or a bare fragment
(`partials/*_fragment.html`) when the request carries the htmx marker header `HX-Request: true` —
this is how htmx's out-of-band swaps get just the changed DOM instead of a full page. Every HTML
route has a `/api/...` JSON twin that returns the identical underlying data
(`fastapi.encoders.jsonable_encoder` over the same dataclass from `web/queries.py`) with no HTML
involved.

If the configured DuckDB file does not exist yet (no writer has flushed a first batch — see G5 in
`docs/ARCHITECTURE.md`), every route renders its documented empty state (zeros, empty lists) rather
than erroring: `web/app.py::get_connection` yields `None` in that case, and every function in
`web/queries.py` has an explicit `if conn is None:` branch.

### `GET /` and `GET /api/overview`

Overview panel: today's cost, token totals by class, active session count, tool success rate, a
14-day cost sparkline, and the 10 most recently-ended sessions.

- **Response shape** (`web/queries.py::OverviewData`): `today_cost_usd` (float), `tokens_input` /
  `tokens_output` / `tokens_cache_read` / `tokens_cache_creation` (int), `active_sessions` (int,
  sessions with `ended_at` within the last 5 minutes), `tool_success_rate` (float or `null`),
  `sparkline` (list of `{day, cost_usd}`, oldest first, 14 entries always present with `0.0` for
  days with no cost), `recent_sessions` (list of `{session_id, started_at, ended_at, cost_usd,
  model}`), `provenance` (see below).
- Status: `200` always (empty-state fallback, never a 404/500 for "no data yet").

### `GET /live` and `GET /api/live`

The live feed: the 50 most recent events across all sources, and sessions active in the last 5
minutes.

- **Response shape** (`LiveData`): `recent_events` (list of `{event_id, ts, source, event_type,
  session_id, tool_name, cost_usd}`, newest first), `active_sessions` (list of `{session_id,
  started_at, last_event_at, running_cost_usd, running_duration_ms}`), `provenance`.
- Status: `200` always.

### `GET /sessions/{session_id}` and `GET /api/sessions/{session_id}`

Per-session task timeline.

- **Path param**: `session_id` (string).
- **Response shape** (`SessionDetail`): `session_id`, `tasks` (list of `TaskRow`:
  `prompt_id` (nullable — events with no `prompt_id`, e.g. compaction, are grouped into one
  synthetic "ungrouped" task row with `prompt_id: null`), `start_ts`, `end_ts`, `duration_ms`,
  `cost_usd`, `tokens_input`, `tokens_output`, `tool_calls_ok`, `tool_calls_fail`, `agent_name`,
  `skill_name`, `langsmith_url` (always `null` currently — the `session_id`↔LangSmith `thread_id`
  join is not wired into this endpoint yet)), `provenance`.
- Status: `200` on a known session; `404` (`{"detail": "unknown session: <id>"}`) if no event in
  `events` has that `session_id` — checked with a direct `SELECT 1 ... LIMIT 1` before doing any
  aggregation work.

### `GET /costs` and `GET /api/costs`

Cost rollups and attribution.

- **Response shape** (`CostsData`): `daily` (list of `{day, cost_usd}`, all days with any cost),
  `weekly` (list of `{week_start, cost_usd}`, `DATE_TRUNC('week', ...)` rollup), `attribution`
  (list of `{dimension, key, cost_usd, event_count}` where `dimension` is `"model"`, `"agent"`,
  `"skill"`, or `"project"` — `"project"` is keyed by `cwd`), `five_metrics` (nullable
  `FiveMetrics`: `task_completion_rate`, `tool_selection_accuracy`, `autonomy_score`,
  `recovery_rate`, `cost_per_successful_task`, each a float or `null`), `provenance`.
- Status: `200` always.

### Provenance footer (embedded in every response above)

Every one of the four responses above carries a `provenance` object
(`web/queries.py::ProvenanceFooter`): `sources` (one entry per `otlp`/`jsonl`/`langsmith` with
`last_seen` timestamp and `event_count`, even if zero), `drift_status` (`"ok"` | `"alert"` |
`"error"` | `"unknown"`, from the latest `drift_check` job run recorded in `events`), and
`job_statuses` (latest run of each named trailing job). This is how a panel answers "where did this
number come from, and how fresh is it" without a separate endpoint.

### `GET /api/events/stream`

Server-Sent Events stream of new events, for the live-updating dashboard.

- **Response**: `text/event-stream` (`sse_starlette.EventSourceResponse`). Each message has
  `event: message` and a JSON `data` payload: `{event_id, ts, source, event_type, session_id,
  tool_name, cost_usd}` (same shape as one `recent_events` row above).
- **Behavior**: polls the read-only store every 0.5s (`_SSE_POLL_INTERVAL_S`) for rows with
  `ts >` the last-seen timestamp, up to 200 rows per poll, ordered oldest-first within a poll. If
  the DB file does not exist yet, the poll loop simply yields nothing until it does. The loop runs
  until the client disconnects (`request.is_disconnected()`), then returns.
- No page size / backfill semantics: a client that connects late gets only events from that point
  forward, not history — `GET /api/live` or `GET /api/overview` are the way to get a snapshot.

### `POST /api/snapshot`

Produces a consistent, point-in-time copy of the live DuckDB file and returns where it landed.

- **Request body**: none.
- **Response** (`200`): `{"path": "<absolute path to the new .duckdb file>", "rows": <int, count(*) from events in the copy>}`.
- **Mechanism**: `store.writer.snapshot(db_path, dest)` (`web/app.py::snapshot_db`) delegates to
  the live `EventWriter`'s `snapshot_to()`, which flushes any buffered events, then uses
  `ATTACH` + `COPY FROM DATABASE` to copy schema, tables, and views atomically. This is the
  supported way to read the DB while `bam serve` is running — DuckDB's file lock is exclusive
  cross-process, so an external `duckdb`/`marimo` process cannot open the live file at all, not
  even read-only (see `docs/ARCHITECTURE.md`).
- **The destination path is always chosen by the server** —
  `settings.store.snapshot_dir / f"bam-{timestamp}.duckdb"`. **This endpoint deliberately does not
  accept a caller-supplied destination.** An endpoint that writes a file to a path the caller
  controls is a file-write primitive regardless of what it's labeled, and the fact that both binds
  are localhost-only (no auth, single-user) is explicitly *not* treated as a reason to build one —
  see `web/app.py::snapshot_db`'s docstring and the same point made in `docs/ARCHITECTURE.md`.
- Every snapshot filename is unique (microsecond timestamp), and `snapshot_to()` refuses to
  overwrite an existing file (`FileExistsError`) as a second-layer guard — not reachable via this
  endpoint's fixed-format filename in practice, but enforced at the function level regardless.

## `bam` CLI

Defined in `src/boss_ai_monitoring/cli.py`; console script entry point `bam = "boss_ai_monitoring.cli:main"`.

### `bam serve`

Loads settings (`config.load_settings()`), builds the one `FastAPI` app
(`cli.py::build_app` — resolves `web.app:create_app` at runtime; falls back to a placeholder app
exposing only `GET /healthz` if `web/app.py` isn't importable, logging a warning rather than
failing silently), and runs **two** `uvicorn.Server` instances — dashboard bind/port and OTLP
bind/port — concurrently in one `asyncio` event loop via `asyncio.gather`. No other command or
module in the codebase constructs a `uvicorn.Config`.

> `bam serve` also starts three supervised background tasks alongside the servers: the JSONL
> scanner, the LangSmith poller (only if `BAM_INGEST__LANGSMITH_PROJECT` is set), and the
> trailing-jobs scheduler. Blocking work is pushed off the event loop with `asyncio.to_thread`, and
> a crashing loop is logged without taking the servers down. See `docs/ARCHITECTURE.md`.

### `bam config [show|db-path]`

Prints resolved configuration (env > YAML > defaults — see `docs/ARCHITECTURE.md` /
`config.py`), never raw environment variables.

- `bam config show` (default if no topic given): the full `BamSettings` as pretty-printed JSON
  (`model_dump_json(indent=2)`).
- `bam config db-path`: prints **only** the resolved DuckDB path, one line, no other output — so
  it composes directly into shell commands: `duckdb "$(uv run bam config db-path)" "..."`. This is
  the documented reason to prefer it over reading `$BAM_DB_PATH` directly (that env var is unset in
  most shells and would silently open an empty database at the literal string `.`/cwd-relative
  path).

### `bam snapshot`

Prints **only** the path to a consistent DuckDB copy — nothing else — so it composes:
`duckdb "$(uv run bam snapshot)" "SELECT ..."`.

- First tries `POST http://<dashboard_bind>:<dashboard_port>/api/snapshot` (30s timeout) and
  prints the `path` from the JSON response.
- If nothing is listening there (`httpx.ConnectError` / `httpx.ConnectTimeout`), falls back to
  snapshotting in-process: opens a short-lived standalone `EventWriter` against the configured
  `db_path` and writes to `settings.store.snapshot_dir / f"bam-{timestamp}.duckdb"` directly.
- Either path produces the same guarantee (a transactionally consistent copy including views), so
  the same command is correct whether or not `bam serve` happens to be running.
