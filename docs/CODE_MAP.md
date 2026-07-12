# Code Map

Module-by-module orientation for a developer about to change `src/boss_ai_monitoring/`. This is
**not** an architecture doc, API reference, or tutorial — see the spec (`specs/boss-ai-monitoring/`)
and its briefs (`specs/boss-ai-monitoring/briefs/`) for those. This doc answers: which file, which
function, what does it own, what will bite me.

Package layout:

```
src/boss_ai_monitoring/
├── config.py              # BamSettings — the only os.environ reader
├── cli.py                 # `bam` console script: serve / config / snapshot
├── store/
│   ├── writer.py           # EventWriter — the single write connection
│   ├── schema.py           # table DDL, EVENT_COLUMNS, view loader
│   └── views.sql           # the six published metric views
├── ingest/
│   ├── otlp.py              # OTLP http/json receiver -> APIRouter
│   ├── jsonl.py             # ~/.claude/projects/**/*.jsonl scanner
│   └── langsmith_poll.py    # LangSmith run poller
├── web/
│   ├── app.py               # create_app(settings) -> FastAPI, all routes
│   ├── queries.py           # read-only query layer backing every panel
│   ├── templates/           # Jinja2 pages + htmx fragments
│   └── static/               # vendored htmx, htmx-sse, style.css
└── jobs/
    ├── scheduler.py          # generic crash-isolating job runner
    ├── correction_scan.py    # heuristic per-session correction score
    ├── drift_check.py        # OTel-vs-JSONL daily drift alerts
    ├── error_classification.py  # deterministic error taxonomy
    ├── live.py               # Wave-3 wiring: DB-backed callables + persistence
    └── _events.py            # `Event = Mapping[str, Any]` type alias (no logic)
```

---

## `config.py`

**Responsibility**: build one `BamSettings` object; the *only* module allowed to touch
`os.environ`.

**Public surface**:

```python
class ConfigError(RuntimeError): ...

class ServerSettings(BaseModel):
    dashboard_port: int = 8000
    dashboard_bind: str = "127.0.0.1"
    otlp_port: int = 4318
    otlp_bind: str = "127.0.0.1"

class StoreSettings(BaseModel):
    db_path: Path = Path("~/.local/share/boss-ai-monitoring/bam.duckdb")
    batch_size: int = 500
    flush_interval_ms: int = 1000
    snapshot_dir: Path = Path("~/.local/share/boss-ai-monitoring/snapshots")

class IngestSettings(BaseModel):
    jsonl_scan_interval_s: int = 15
    claude_projects_dir: Path = Path("~/.claude/projects")
    langsmith_poll_interval_s: int = 60
    langsmith_project: str | None = None
    otlp_max_body_bytes: int = 10 * 1024 * 1024

class JobsSettings(BaseModel):
    correction_scan_enabled: bool = True
    drift_check_enabled: bool = True
    error_classification_enabled: bool = True
    interval_s: int = 300
    jitter_s: int = 30

class BamSettings(BaseSettings):
    server: ServerSettings
    store: StoreSettings
    ingest: IngestSettings
    jobs: JobsSettings

def resolve_config_path(config_path: Path | str | None = None) -> Path
def load_settings(config_path: Path | str | None = None) -> BamSettings
```

**Owns**: precedence resolution — env (`BAM_` prefix, `__` nesting, via
`settings_customise_sources` putting `env_settings` first) > YAML (`./config.yaml`, or
`$BAM_CONFIG`, or an explicit arg) > field defaults. Also owns two flat legacy aliases,
`BAM_DB_PATH` and `BAM_CLAUDE_PROJECTS_DIR` (`FLAT_ENV_ALIASES`), folded in as YAML-priority init
kwargs — so the nested `BAM_STORE__DB_PATH` spelling always wins over the flat alias if both are
set.

**Gotcha**: `StoreSettings.snapshot_dir` is derived in a `model_validator(mode="before")`
(`_snapshot_dir_follows_db_path`) as `db_path.parent / "snapshots"` whenever it isn't explicitly
set — computed *before* field validation so the field stays a plain `Path`, never `Path | None`.
Do not hardcode a home-relative default for this field: in Docker the DB lives on the
`bam_data:/data` volume (`BAM_STORE__DB_PATH=/data/bam.duckdb`), and a hardcoded snapshot default
would put snapshots on the container's ephemeral filesystem, outside the volume — gone on restart.
If you add a new path-shaped setting, decide explicitly whether it should follow `db_path` the
same way.

Also: `_as_absolute_path` deliberately does **not** call `.resolve()` — that would rewrite `/tmp`
to `/private/tmp` on macOS and make `bam config db-path` disagree with what the user configured.

---

## `cli.py`

**Responsibility**: the `bam` console script (`pyproject.toml` `[project.scripts]`). Three
subcommands, `serve` / `config` / `snapshot`.

**Public surface**:

```python
def build_app(settings: BamSettings) -> FastAPI
async def _serve_both(app: FastAPI, settings: BamSettings) -> None
def build_parser() -> argparse.ArgumentParser
def main(argv: Sequence[str] | None = None) -> int
```

**Owns**: port wiring. `bam serve` is **one** `FastAPI` app object (resolved via
`build_app` -> `web.app.create_app`) served by **two** `uvicorn.Server` instances
(`settings.server.dashboard_bind:dashboard_port` and `otlp_bind:otlp_port`) inside **one**
`asyncio.gather` in `_serve_both`. Nobody else in the codebase is allowed to construct a
`uvicorn.Server` or bind a port.

`build_app` resolves `boss_ai_monitoring.web.app` via `importlib.import_module` rather than a
static import, and falls back to a placeholder `FastAPI` app (`/healthz` only) if that module
isn't present — a leftover scaffold-wave shim now that `web/app.py` exists, but harmless; the
`ModuleNotFoundError` fallback only triggers if `web.app`'s own imports fail, in which case the
warning is loud (`log.warning`), never silent.

**Gotcha**: `bam config db-path` prints `settings.store.db_path` resolved through the full
precedence chain. Never shell out to `$BAM_DB_PATH` directly — it is unset in most shells (the
canonical spelling is `BAM_STORE__DB_PATH`) and a bare reference silently opens/creates an empty
DB at the default path instead of erroring. Always run
`duckdb "$(uv run bam config db-path)" ...`.

`bam snapshot` (`_cmd_snapshot`) tries `POST /api/snapshot` on the running app first (`httpx.post`,
30s timeout); only on `ConnectError`/`ConnectTimeout` does it fall back to an in-process
`store.writer.snapshot()` call. Prints **only** the resulting path (or nothing on failure) so it
composes in a pipeline — don't add other stdout output to this command.

---

## `store/writer.py`

**Responsibility**: `EventWriter` is *the* single DuckDB write connection (G5). Buffers events,
flushes in one atomic + idempotent transaction. Everyone else reads via `connect_read_only`.

**Public surface**:

```python
class EventWriter:
    def __init__(self, db_path: Path, *, batch_size: int = 500, flush_interval_ms: int = 1000) -> None
    def write(self, event: Event) -> None
    def write_many(self, events: Iterable[Event]) -> int
    def flush(self) -> int
    def snapshot_to(self, dest: Path) -> Path
    def close(self) -> None
    def get_cursor(self, source: str, key: str) -> str | None
    def set_cursor(self, source: str, key: str, cursor: str) -> None

def snapshot(db_path: Path, dest: Path) -> Path
def get_writer(settings: BamSettings) -> EventWriter          # process-wide singleton, keyed by db_path
def connect_read_only(db_path: Path) -> duckdb.DuckDBPyConnection
```

`Event = dict[str, Any]`, one dict per row shaped like `EVENT_COLUMNS` (`store/schema.py`).

**Owns**: the write connection lifecycle, the batch buffer, the `_events_staging` temp table used
for the anti-join flush, the `ingest_cursors` table (resume points, keyed by
`(source, key)`), and the `_writers` process-wide registry (`dict[str, EventWriter]`, keyed by
`str(db_path)`, guarded by `_writers_lock`).

**Three load-bearing subtleties** — read these before touching this file:

1. **`flush()` holds `self._lock` across the whole DB round-trip**, not just the buffer swap.
   Releasing the lock before `BEGIN TRANSACTION` let two concurrent producers (OTLP handler, JSONL
   scanner, LangSmith poller — all calling through the same `get_writer()` singleton) both pass the
   buffer swap and then race `BEGIN TRANSACTION` on the *one shared connection*. `snapshot_to()`
   takes the same lock for the same reason: the whole `ATTACH` + `COPY FROM DATABASE` round-trip
   needs to be serialized against concurrent writers, not just protected at the swap boundary.

2. **`connect_read_only()` is writer-aware.** DuckDB refuses a second `duckdb.connect(path,
   read_only=True)` while a read-write connection to the same file is already open in-process —
   exactly the state `bam serve` is in from the moment `get_writer()`'s singleton goes live. So
   `connect_read_only` first checks the `_writers` registry: if a writer for that path is live, it
   hands back `writer._conn.cursor()` off the *same* connection (a fresh session via MVCC — sees
   only committed rows, never blocks or is blocked by the writer's in-flight transaction) instead
   of a fresh `duckdb.connect`. `pin_utc()` is called on the cursor too, because a cursor has its
   own session settings, not inherited from the parent connection. Falls back to a genuine
   read-only `duckdb.connect` only when no writer for that path is live (tests, standalone marimo).
   `EventWriter.close()` evicts itself from `_writers` under `_writers_lock` specifically so this
   path can't hand out a cursor on an already-closed connection.

3. **`snapshot_to()` / module-level `snapshot()`** exist because DuckDB's file lock is *exclusive
   cross-process*: while `bam serve` holds the write connection, no outside `duckdb` CLI or marimo
   process can open the file at all, not even read-only. So the only way to get a consistent copy
   out is to ask the process that already holds the connection to make one, via `ATTACH ... AS
   bam_snapshot` + `COPY FROM DATABASE "<name>" TO bam_snapshot` on the writer's own connection
   (schema, tables, *and* views — not a raw file copy, which could produce torn bytes). Buffered
   events are flushed first or the snapshot silently omits the newest rows. The module-level
   `snapshot(db_path, dest)` helper picks the live writer from the registry if one exists, else
   spins up and tears down a short-lived standalone `EventWriter` — this is what `bam snapshot`'s
   in-process fallback and `tests/unit/store/test_snapshot.py` use.

**Gotcha**: `snapshot_to` raises `FileExistsError` if `dest` already exists — callers must always
generate a fresh timestamped filename (see `cli.py::_cmd_snapshot` and
`web/app.py::snapshot_db`, both stamp `datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")`).

---

## `store/schema.py`

**Responsibility**: table DDL, the canonical column list, and the view loader.

**Public surface**:

```python
EVENT_COLUMNS: tuple[str, ...]   # 21 columns, shared source of truth with writer.py's INSERT

def pin_utc(conn: duckdb.DuckDBPyConnection) -> None
def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None   # idempotent, CREATE TABLE IF NOT EXISTS
def load_views(conn: duckdb.DuckDBPyConnection) -> None      # re-applies views.sql verbatim
```

`EVENT_COLUMNS` order: `event_id, ts, source, event_type, session_id, prompt_id, request_id,
model, git_sha, agent_name, skill_name, tool_name, cost_usd, duration_ms, tokens_input,
tokens_output, tokens_cache_read, tokens_cache_creation, success, cwd, payload`. This tuple drives
both the `CREATE TABLE events (...)` DDL *and* `writer.py`'s parameterized `INSERT` column list —
**the two must stay in lockstep**; adding a column means editing the DDL string in this file too
(there's no migration system, `CREATE TABLE IF NOT EXISTS` only helps on a fresh DB).

`events` also has a `PRIMARY KEY (event_id)`. The second table, `ingest_cursors`, is
`(source, key) PRIMARY KEY`, columns `cursor VARCHAR`, `updated_at TIMESTAMP`.

**Gotcha**: `pin_utc` (`SET TimeZone='UTC'`) must run on *every* connection before its first
read/write, because `ts`/`updated_at` are plain `TIMESTAMP` (not `TIMESTAMPTZ` — that would need
the `pytz` package this project doesn't depend on), and DuckDB silently converts an incoming
tz-aware Python `datetime` to the session's local wall clock before storing it as naive. `writer.py`
pins it in `EventWriter.__init__` and again on every cursor handed out by `connect_read_only`;
`store/schema.py::ensure_schema` also pins it. If you open a DuckDB connection anywhere new, call
`pin_utc` on it first or timestamps will silently shift by the host's UTC offset.

---

## `store/views.sql`

Applied via `schema.py::load_views` with `CREATE OR REPLACE VIEW` — safe to re-apply on every
boot, always reflects the file in the repo (no drift between deployed views and source).

| View | Answers |
|---|---|
| `v_cost_events` | (helper, not a panel view) `events` rows where `cost_usd IS NOT NULL`, minus `source='jsonl'` rows that have a matching `otlp` row on `(request_id, session_id)` — the G6 estimate-vs-real dedupe. Every cost-bearing view below is built on top of this. |
| `v_sessions` | Start/end/duration/cost/model, one row per `session_id`. |
| `v_tasks` | Start/end/duration/cost/tokens/`tool_call_count`, one row per `prompt_id` (= one user prompt = one task). |
| `v_costs_daily` | Cost + event count per UTC calendar day (`CAST(ts AS DATE)` — safe because `ts` is always UTC wall-clock per `pin_utc`). |
| `v_tool_stats` | Per `tool_name`: call count, success rate, p50/p95 `duration_ms` (`quantile_cont`). |
| `v_attribution` | Cost + avg duration + event count per `(agent_name, skill_name, model)`. Excludes `event_type = 'job_run'` rows (scheduler bookkeeping — those have all three columns `NULL` and would otherwise inflate the `(NULL, NULL, NULL)` bucket). |
| `v_five_metrics` | Single summary row: `task_completion_rate`, `tool_selection_accuracy`, `autonomy_score`, `recovery_rate`, `cost_per_successful_task` — the "5 metrics that matter." |

**Gotcha**: `v_attribution`'s `LEFT JOIN` between `attributed_stats` and `attributed_costs` uses
`IS NOT DISTINCT FROM` on all three grouping columns, not `=` — required because `agent_name` /
`skill_name` are frequently `NULL` and `NULL = NULL` is `NULL` (row dropped), not `TRUE`.

---

## `ingest/otlp.py`

**Responsibility**: OTLP http/json receiver. G1: http/json only, no gRPC, no otel-collector, ever.

**Public surface**:

```python
def get_router() -> APIRouter        # POST /v1/logs, POST /v1/metrics
def parse_logs_payload(data: dict[str, Any]) -> list[Event]
def parse_metrics_payload(data: dict[str, Any]) -> list[Event]
```

**Owns**: the OTLP JSON parse tree (`resourceLogs -> scopeLogs -> logRecords`, and the metrics
equivalent `resourceMetrics -> scopeMetrics -> metrics -> dataPoints`), the
`_ATTR_TO_COLUMN` attribute-key -> canonical-column mapping, gzip body decoding, and the 400/413
error responses. `get_router()` is mounted into the one app object at the `# otlp-mount` region in
`web/app.py::create_app` — router internals stay owned here, `web` only owns the mount call.

**Idempotency**: `event_id = sha256(source|session_id|timeUnixNano|json.dumps(record))` — fully
deterministic on the record's own content, so replaying an export batch reproduces identical ids
and `EventWriter`'s anti-join flush makes the replay a no-op.

**Gotcha**: any attribute key not in `_ATTR_TO_COLUMN` lands verbatim in the `payload` JSON column
instead of being dropped — new OTel attributes or entirely new `event.name` values need **no code
change here**, let alone a schema migration, to be captured (they just won't be queryable as a
first-class column until someone adds the mapping). A malformed *outer* body (not even a JSON
object, or invalid JSON/gzip) is a 400 before `parse_logs_payload`/`parse_metrics_payload` ever run;
everything inside a well-formed outer body is parsed defensively (`.get(..., [])` throughout) —
missing substructure yields fewer events, never a crash. `_apply_attrs`'s coercion (e.g. `cost_usd`
-> `float`) also degrades on failure: a bad value falls through to `payload` raw rather than
raising. `event_type` for a log record with no `event.name` attribute is the literal string
`"unknown"`, not `None` — filter on that if you need "malformed/unrecognized" rows.

---

## `ingest/jsonl.py`

**Responsibility**: incremental `~/.claude/projects/**/*.jsonl` scanner — historical backfill and
the safety net for whatever happened while the OTLP endpoint was down. Reverse-engineered format
(no published schema); every parse decision here is best-effort.

**Public surface**:

```python
def parse_line(raw_line: str, state: dict[str, _SessionState]) -> list[Event]   # never raises
def scan_once(projects_dir: Path, writer: EventWriter) -> ScanStats
async def run_forever(projects_dir: Path, writer: EventWriter, *, interval_s: float,
                       iterations: int | None = None,
                       sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None

@dataclass
class ScanStats:
    files_scanned: int = 0
    events_written: int = 0
```

**Owns**: per-file byte-offset cursors (`writer.get_cursor("jsonl", str(path))`,
persisted via `writer.set_cursor`), the `_SessionState` bookkeeping (active `prompt_id`, `cwd`,
`git_sha`, pending `tool_use` id -> name map) threaded across lines of one scan pass, and the
`user`/`assistant` transcript-entry -> event mapping (`api_request`, `tool_result`,
`user_prompt`, `compaction`).

**Gotcha (truncation/rotation)**: `scan_once` compares the persisted cursor offset to the file's
current size; if `size < offset` the file is treated as truncated-or-rotated-in-place and the scan
**restarts from byte 0**, not from the stored offset. If your test fixture shrinks a file between
scans without expecting a full re-read, this is why.

**Gotcha (prompt_id heuristic)**: `_SessionState.prompt_id` is threaded forward — only
user-authored lines (`entry.get("promptId")`) update it; assistant lines never carry their own
`promptId` in real transcripts, so every line after a given `user` line inherits that
`prompt_id` until the next one arrives. This is a documented best-effort assumption
(`OQ-jsonl-01`), not a guarantee from the format.

**Gotcha (cost dedupe)**: JSONL costs are **estimates**. They are deduped against OTel-derived
costs in SQL, not in this module — `store/views.sql`'s `v_cost_events` excludes a
`source='jsonl'` row whenever a matching `source='otlp'` row exists with the same
**`request_id` + `session_id`** (`prompt_id` is *not* part of the join key — a task/prompt can span
multiple requests). If you're chasing a "duplicate cost" bug, check `v_cost_events`, not this file.

`run_forever` never lets one bad pass kill the loop: `scan_once` exceptions are caught and logged
(`logger.exception`), and the next interval retries.

---

## `ingest/langsmith_poll.py`

**Responsibility**: pulls back down what the `langsmith-claude-code-plugins` hook already pushed
up to LangSmith. Enrichment, not a dependency — degrades visibly, never crashes, on a
missing/invalid API key (G14).

**Public surface**:

```python
async def poll_once(project_name: str, writer: EventWriter, *, client: AsyncClient | None = None,
                     cursor_key: str | None = None, max_retries: int = 5, limit: int = 200,
                     sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> PollResult
async def run_forever(project_name: str, writer: EventWriter, *, interval_s: float,
                       iterations: int | None = None, client: AsyncClient | None = None,
                       sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None

@dataclass
class PollResult:
    status: Literal["ok", "disabled", "error"]
    runs_seen: int = 0
    events_written: int = 0
    unmatched_runs: int = 0
    detail: str | None = None
```

**Owns**: the cursor-based poll window (persisted via `writer.set_cursor("langsmith", key, ...)`,
default key is `project_name`), rate-limit backoff (`2**attempt` seconds, up to `max_retries`,
catching `ls_utils.LangSmithRateLimitError`), and the `thread_id` <-> `session_id` join.

**Gotcha (auth is never read here)**: this module never touches `os.environ`. It inspects the SDK
client's own resolved `.api_key` property (`if not client.api_key: return PollResult(status=
"disabled", ...)`) — auth is ambient via direnv (`LANGSMITH_API_KEY`), and reading it directly
here would violate config.py's monopoly on `os.environ`.

**Gotcha (first-poll lookback)**: with no persisted cursor, the first poll only reaches back
`_DEFAULT_LOOKBACK = timedelta(days=7)`, not "forever" — an unbounded `start_time` was observed in
manual E2E to trigger sustained 429s (the API's rate limit is scoped to <=7-day windows). Deep
history backfill beyond 7 days is explicitly not this poller's job.

**Gotcha (cursor overlap)**: every successful poll sets the next cursor to
`latest_start - _CURSOR_OVERLAP` (60s), deliberately re-requesting the last minute of the previous
window on every pass. Safe because `event_id = f"{SOURCE}:{run.id}"` and the writer dedupes on
`event_id` — reprocessing the overlap is a no-op, not a duplicate row.

**Gotcha (join is best-effort and visible)**: a run's `session_id` is set **only** if its
`metadata["thread_id"]` exactly matches a `session_id` already present in the local `events` table
(`_known_session_ids`, queried through `writer._conn` directly — not a fresh `connect_read_only`
call, because DuckDB refuses a second read-only connection while the writer's read-write one is
open **in this exact code path**, before `store.writer.connect_read_only`'s writer-aware fix would
even apply since this reads through the writer object it was handed, not through that function).
Anything that doesn't match lands with `session_id = NULL` and increments `unmatched_runs` — a
visible "LangSmith-only" bucket, never silently merged into an unrelated session.

---

## `web/app.py`

**Responsibility**: `create_app(settings) -> FastAPI` — every route, the SSE live feed, and the
`/api/snapshot` endpoint. `web` never opens a write connection (G5) and never creates the DuckDB
file; if it doesn't exist yet, every route degrades to its empty state.

**Public surface**:

```python
def create_app(settings: BamSettings) -> FastAPI
def get_connection(request: Request) -> Iterator[duckdb.DuckDBPyConnection | None]  # FastAPI dependency
Connection = Annotated[duckdb.DuckDBPyConnection | None, Depends(get_connection)]
```

Routes registered inside `create_app`:

| Method | Path | Returns |
|---|---|---|
| GET | `/` | `overview.html` full page, or `partials/overview_fragment.html` if `HX-Request: true` |
| GET | `/api/overview` | JSON twin of the above (`OverviewData`) |
| GET | `/live` | `live.html` / `partials/live_fragment.html` |
| GET | `/api/live` | JSON twin (`LiveData`) |
| GET | `/sessions/{session_id}` | `session_detail.html` / fragment, 404 if unknown |
| GET | `/api/sessions/{session_id}` | JSON twin (`SessionDetail`), 404 if unknown |
| GET | `/costs` | `costs.html` / fragment |
| GET | `/api/costs` | JSON twin (`CostsData`) |
| GET | `/api/events/stream` | `EventSourceResponse` — SSE live feed |
| POST | `/api/snapshot` | `{"path": ..., "rows": ...}` — triggers a fresh snapshot |

Every HTML route follows the same **htmx fragment + JSON twin** pattern: `_is_fragment_request`
checks the `HX-Request` header to pick full-page vs. fragment template; the `/api/*` route calls
the exact same `queries.get_*` function and returns it via `jsonable_encoder`. If you add a panel,
add both the HTML route and its `/api/*` JSON twin, or the dashboard and the JSON API drift apart.

**Owns**: the `# otlp-mount` marker region (mounts `ingest.otlp.get_router()` — router internals
stay owned by `ingest/otlp.py`, `web` only owns the `app.include_router(...)` call itself), the SSE
poll loop (`_poll_events`, 0.5s interval, plain `SELECT ... WHERE ts > ?` against
`connect_read_only` — no cross-module event bus), and the `/api/snapshot` destination path
(`settings.store.snapshot_dir / f"bam-{stamp}.duckdb"` — **always** server-chosen, never taken from
the caller; an endpoint that writes to a caller-supplied path would be a file-write primitive).

**Gotcha**: `get_connection` yields `None` (not an exception, not an empty-but-real connection) when
`settings.store.db_path` doesn't exist on disk yet. Every `queries.get_*` function has an explicit
`if conn is None:` branch for this — new query functions must follow the same contract or the
dashboard will 500 on a fresh install before the writer's first flush.

---

## `web/queries.py`

**Responsibility**: the read-only query layer every panel and its JSON twin calls through. Reads
the six published views (`v_sessions`, `v_tasks`, `v_costs_daily`, `v_tool_stats`,
`v_attribution`, `v_five_metrics`, plus the helper `v_cost_events`) and supplements a few fields no
view covers by querying `events` directly (live feed, per-source freshness, per-prompt
agent/skill + tool success/fail split, the "no `prompt_id`" task bucket).

**Public surface**:

```python
def get_provenance_footer(conn: duckdb.DuckDBPyConnection | None) -> ProvenanceFooter
def get_overview(conn: duckdb.DuckDBPyConnection | None, *, now: datetime) -> OverviewData
def get_live(conn: duckdb.DuckDBPyConnection | None, *, now: datetime) -> LiveData
def get_session_detail(conn: duckdb.DuckDBPyConnection | None, session_id: str) -> SessionDetail | None
def get_five_metrics(conn: duckdb.DuckDBPyConnection | None) -> FiveMetrics | None
def get_costs(conn: duckdb.DuckDBPyConnection | None) -> CostsData
```

Every function takes `conn: duckdb.DuckDBPyConnection | None` and every function has a `None`
branch returning its type's empty shape (not `None` itself, except `get_session_detail`/
`get_five_metrics` which return `None` for "no data"/"unknown session"). Result types are frozen
dataclasses: `SourceFreshness`, `JobStatus`, `ProvenanceFooter`, `DailyCost`, `WeeklyCost`,
`SessionSummary`, `OverviewData`, `EventSummary`, `ActiveSessionCard`, `LiveData`, `TaskRow`,
`SessionDetail`, `AttributionRow`, `FiveMetrics`, `CostsData`.

**Owns**: `ProvenanceFooter` — the source-lineage/freshness/drift/job-status strip rendered on
every panel (`partials/provenance_footer.html`). Reads `event_type='job_run'` rows written by
`jobs/live.py::persist_job_status` (`json_extract_string(payload, '$.name'/'$.status')`,
`QUALIFY row_number() OVER (PARTITION BY job_name ORDER BY ts DESC) = 1`) — this is the *only*
place that shape is decoded on the read side.

**Gotcha**: the drift-status `QUALIFY row_number() ... = 1` query over zero matching rows still
returns **one** row of all-`NULL`s (a DuckDB quirk with unpartitioned window functions), not zero
rows — `get_provenance_footer` explicitly checks `if drift_status_value is None:` to map that case
to `"unknown"` rather than crashing on `assert drift_row is not None` followed by an unpacking
error. If you add a similar "latest row per key" query, this NULL-row-not-empty-result behavior
will surprise you too.

---

## `web/templates/` and `web/static/`

Jinja2Templates root is `web/templates/`. Full pages: `base.html`, `overview.html`, `live.html`,
`costs.html`, `session_detail.html`. htmx fragments (returned when `HX-Request: true`) live under
`templates/partials/`: `overview_fragment.html`, `live_fragment.html`, `costs_fragment.html`,
`session_detail_fragment.html`, plus the shared `provenance_footer.html` included by every page.

`web/static/` is mounted at `/static` (`StaticFiles`). Contains vendored `htmx.min.js`,
`htmx-sse.js`, and `style.css` — no npm, no build step, no CDN fetch. Adding a JS dependency means
vendoring the file here.

---

## `jobs/scheduler.py`

**Responsibility**: generic, job-agnostic asyncio runner — enable/disable per job, per-job crash
isolation, jittered interval. Knows nothing about correction scans, drift, or errors specifically.

**Public surface**:

```python
JobCallable = Callable[[], object] | Callable[[], Awaitable[object]]

@dataclass(frozen=True)
class JobDefinition:
    name: str
    enabled: bool
    func: JobCallable

@dataclass(frozen=True)
class JobRunResult:
    name: str
    status: Literal["ok", "error"]
    started_at: datetime
    finished_at: datetime
    error: str | None = None
    result: object | None = None

class JobScheduler:
    def __init__(self, jobs: Iterable[JobDefinition], *, interval_s: float, jitter_s: float = 0.0,
                 clock=..., sleep=..., rand=..., persist: Callable[[JobRunResult], None] | None = None) -> None
    @classmethod
    def from_settings(cls, settings: JobsSettings, callables: dict[str, JobCallable], *,
                       clock=..., sleep=..., rand=..., persist=None) -> JobScheduler
    async def run_once(self) -> list[JobRunResult]
    def last_status(self, name: str) -> JobRunResult | None
    async def run_forever(self, *, iterations: int | None = None) -> None
```

`from_settings` gates each named callable's `enabled` flag via
`_DEFAULT_ENABLE_FLAG_BY_NAME = {"correction_scan": "correction_scan_enabled", "drift_check":
"drift_check_enabled", "error_classification": "error_classification_enabled"}` — a callable whose
name isn't in that map defaults to `enabled=True` (`getattr(settings, ..., True)`).

**Gotcha**: a disabled job is skipped entirely in `run_once` — no `JobRunResult`, no `last_status`
entry, nothing passed to `persist`. Don't confuse "disabled" with "ran and succeeded" when reading
`last_status(name) is None`; it also means "never ran yet" or "not in the callables dict at all."

A job whose `func()` raises never propagates out of `run_once` — the exception is caught and
turned into a `status="error"` `JobRunResult` with `error=str(exc)`, so one broken job never takes
down the scheduler or its siblings. If `persist` itself raises, that's *also* swallowed
(`contextlib.suppress(Exception)`) — a broken persistence sink must not lose the run outcome from
`last_status`, even though it won't reach the store either.

---

## `jobs/correction_scan.py`, `jobs/drift_check.py`, `jobs/error_classification.py`

Pure, hermetic, **deterministic/heuristic-only functions — no LLM judge, deliberately (G9)**.
Each takes `events: Iterable[Event]` (`Event = Mapping[str, Any]`, `jobs/_events.py`) and returns a
list of frozen dataclasses. No I/O, no DuckDB import — this is why they're trivially unit-testable
against fixture event lists (`tests/unit/jobs/`).

```python
# correction_scan.py
CORRECTION_PHRASES: tuple[str, ...]           # substring match, lowercased
DEFAULT_REPROMPT_WINDOW_S = 60.0
def scan_corrections(events: Iterable[Event], *, reprompt_window_s: float = 60.0) -> list[SessionCorrectionScore]

@dataclass(frozen=True)
class SessionCorrectionScore:
    session_id: str
    prompt_count: int
    correction_count: int
    phrase_matches: int
    reprompt_matches: int
    @property
    def score(self) -> float   # correction_count / prompt_count, 0.0 if no prompts
```

```python
# drift_check.py
DEFAULT_THRESHOLD_PCT = 0.05
def check_drift(events: Iterable[Event], *, threshold_pct: float = 0.05) -> list[DriftAlert]

@dataclass(frozen=True)
class DriftAlert:
    day: date
    metric: str            # "session_count" | "token_total"
    otlp_value: float
    jsonl_value: float
    drift_pct: float        # abs(otlp - jsonl) / max(otlp, jsonl); alert only if > threshold
```

```python
# error_classification.py
TAXONOMY: dict[str, tuple[str, ...]]   # rate_limit / timeout / auth / network / validation, substring match
def classify_error_type(error_type: str | None) -> str    # -> a TAXONOMY key, "other", or "unknown"
def classify_errors(events: Iterable[Event]) -> list[ErrorClassificationRow]

@dataclass(frozen=True)
class ErrorClassificationRow:
    category: str
    error_type: str | None
    event_type: str
    count: int
```

**Gotcha (correction_scan)**: a "correction" is scored per `user_prompt` event via two
independent signals — a literal phrase match against `payload["text"]`, OR the prompt arriving
within `reprompt_window_s` of the *most recent failed* `tool_result` in the same session (a
reprompt after a tool failure). Either signal alone counts the prompt once; `phrase_matches` and
`reprompt_matches` are tracked separately for diagnostics but `correction_count` is not their sum.

**Gotcha (drift_check)**: `_drift_pct` divides by `max(otlp_value, jsonl_value)`, not by
`otlp_value` — so a 100%-vs-0% mismatch (one source completely silent) is `drift_pct = 1.0`
regardless of which side is zero, and `largest == 0` (both sources silent that day) is explicitly
`0.0` drift, not a divide-by-zero.

**Gotcha (error_classification)**: `classify_errors` only looks at `event_type == "api_error"` OR
(`event_type == "tool_result"` AND `success is False`) — a `tool_result` with `success=None`
(unknown) is silently excluded, not bucketed as `"unknown"`. `classify_error_type(None)` and
`classify_error_type("")` both return `"unknown"`; a non-empty string matching no taxonomy needle
returns `"other"` — these are three different outcomes, don't conflate them when reading the
rollup.

---

## `jobs/live.py`

**Responsibility**: Wave-3 wiring — turns the three hermetic functions above into DuckDB-backed
`JobCallable`s, and persists/reads back `JobRunResult`s as ordinary `events` rows.

**Public surface**:

```python
def fetch_events(conn: duckdb.DuckDBPyConnection) -> list[Event]
def run_correction_scan(conn: duckdb.DuckDBPyConnection) -> list[SessionCorrectionScore]
def run_drift_check(conn: duckdb.DuckDBPyConnection, *, threshold_pct: float = DEFAULT_THRESHOLD_PCT) -> list[DriftAlert]
def run_error_classification(conn: duckdb.DuckDBPyConnection) -> list[ErrorClassificationRow]
def build_live_callables(db_path: Path) -> dict[str, JobCallable]
def persist_job_status(writer: EventWriter, result: JobRunResult) -> None
def read_job_status(conn: duckdb.DuckDBPyConnection, job_name: str) -> dict[str, Any] | None

JOB_RUN_EVENT_TYPE = "job_run"
JOB_RUN_SOURCE = "jobs"
```

`build_live_callables` wraps each `run_*` function so every scheduler pass opens its **own**
short-lived `connect_read_only(db_path)` connection and closes it after — a fresh snapshot of
`events` per job run, never held open across the sleep interval.

`persist_job_status` writes a **new** `events` row per run (`event_id` includes
`finished_at.isoformat()`, so it's always unique — append-only, never an `UPDATE`), with
`agent_name`/`skill_name`/`model`/`cost_usd` all `None`. That's deliberate: it keeps job-run rows
out of `v_cost_events` (`cost_usd IS NOT NULL` filter) and out of any per-agent/skill/model cost
breakdown, while `drift_check`'s result additionally gets `payload["alert_count"] = len(result)` —
`web/queries.py::get_provenance_footer` reads exactly this field to decide `drift_status = "alert"`.

**Gotcha — read this before assuming background jobs run in production**: `build_live_callables`
+ `JobScheduler.from_settings` are never actually wired together anywhere in this codebase.
Grepping `cli.py` and `web/app.py` for `JobScheduler`, `build_live_callables`, `run_forever`,
`scan_once`, or `poll_once` returns nothing — `bam serve` starts the FastAPI app and the two
uvicorn binds and nothing else. The JSONL scanner, the LangSmith poller, and the trailing-job
scheduler all have working `run_forever` loops, but **no caller starts any of the three
background loops as part of `bam serve`.** The module's own docstring documents this as
intentional-for-now, citing `OQ-04` ("duckdb refuses a `read_only=True` connection while a
read-write one is open in the same process") as the blocker preventing a one-call
`build_scheduler(settings)` helper — **but that specific blocker is already fixed**:
`store/writer.py::connect_read_only` has a writer-aware branch that hands out a `.cursor()` off
the live writer's connection instead of failing, and
`tests/unit/store/test_writer.py::test_connect_read_only_coexists_with_a_live_writer` (plus two
neighboring tests) proves it works concurrently with a live writer. If you're picking up wiring
JSONL/LangSmith/job-scheduling into `bam serve`, the `OQ-04` objection in this docstring is stale
and the actual remaining work is just calling `asyncio.gather` (or `asyncio.create_task`) on
`jsonl.run_forever`, `langsmith_poll.run_forever`, and
`JobScheduler.from_settings(...).run_forever()` alongside `_serve_both` in `cli.py`.

`jobs/_events.py`'s `Event = Mapping[str, Any]` alias is otherwise dead weight now that
`store/schema.py` exists — its docstring says as much ("a local stub... NOT a new source of
truth"); it's still imported by every `jobs/*.py` module purely for the type alias, not for any
logic.

---

## Tests layout

```
tests/
├── conftest.py                     # top-level fixtures
├── test_smoke.py
├── unit/
│   ├── store/                      # writer.py, schema.py, views.sql (test_writer/_schema/_views/_snapshot.py)
│   ├── ingest/                     # otlp.py, jsonl.py, langsmith_poll.py
│   ├── web/                        # app.py, queries.py (conftest overrides get_connection)
│   ├── jobs/                       # scheduler.py + the three hermetic job modules + live.py
│   └── hooks/                      # .claude/hooks/pyrefly_session_scope.py
├── integration/                    # empty so far
├── e2e/
│   └── test_dashboard.py           # exercises the real create_app() wiring, not overridden fixtures
└── fixtures/
    ├── otlp/       # *.json ExportLogsServiceRequest / ExportMetricsServiceRequest bodies
    ├── jsonl/       # *.jsonl transcript samples (malformed line, growing file, unicode, subagent, compaction)
    ├── langsmith/   # (currently empty — .gitkeep only)
    └── duckdb/      # (currently empty — .gitkeep only)
```

`tests/unit/web/test_app.py` and `test_queries.py` drive routes through
`app.dependency_overrides[get_connection]` (see `tests/unit/web/conftest.py`) — production wiring
(the real `settings.store.db_path.exists()` branch) is exercised only by
`tests/e2e/test_dashboard.py`.

---

## Where do I change X?

| I want to... | Touch these files |
|---|---|
| Add a new event field | `store/schema.py` (`EVENT_COLUMNS` + the `CREATE TABLE events` DDL string) — keep both in lockstep; then whichever ingest module(s) should populate it (`ingest/otlp.py`'s `_ATTR_TO_COLUMN`, `ingest/jsonl.py`'s `_make_event` calls, or `ingest/langsmith_poll.py`'s `_run_to_event`); then `store/views.sql` if a view should surface it |
| Add/change a metric view | `store/views.sql` only (`CREATE OR REPLACE VIEW`, re-applied every boot by `schema.py::load_views`) |
| Add a dashboard panel | `web/queries.py` (new `get_*` function + dataclass), `web/app.py` (HTML route + `/api/*` JSON twin, both calling the same query function), `web/templates/<page>.html` + `web/templates/partials/<page>_fragment.html` |
| Change what's in the provenance footer | `web/queries.py::get_provenance_footer` + `web/templates/partials/provenance_footer.html` |
| Support a new ingest source | New module under `ingest/`, write via `store.writer.get_writer(settings)` / `EventWriter.write_many`, own its cursor via `writer.get_cursor`/`set_cursor` with a new `source` string, add it to `web/queries.py::SOURCES` so the provenance footer picks it up, and wire its `run_forever` loop into `bam serve` (currently nothing does this even for the existing three — see the `jobs/live.py` gotcha above) |
| Change config precedence or add a setting | `config.py` only — add the field to the right `*Settings` model; never read `os.environ` anywhere else |
| Change how `bam serve` binds ports | `cli.py::_serve_both` + `config.py::ServerSettings` |
| Change snapshot behavior | `store/writer.py` (`EventWriter.snapshot_to`, module-level `snapshot`) is the only place that does the `ATTACH`/`COPY FROM DATABASE`; `cli.py::_cmd_snapshot` and `web/app.py::snapshot_db` are both thin callers |
| Add/change a trailing job | New pure function in `jobs/<name>.py` (events in, dataclass rows out, no I/O) for testability, then wire it into `jobs/live.py::build_live_callables` and `_DEFAULT_ENABLE_FLAG_BY_NAME` in `jobs/scheduler.py`, then add the corresponding `*_enabled: bool` to `config.py::JobsSettings` |
| Add a static JS/CSS asset | Vendor the file directly into `web/static/` — no npm, no build step, no CDN |
