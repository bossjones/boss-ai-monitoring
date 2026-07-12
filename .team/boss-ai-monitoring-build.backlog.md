# Backlog — boss-ai-monitoring-build

APPEND-ONLY. Any pane appends a new entry. Only the LEAD edits or triages existing entries.
Never rewrite another pane's entry.

Entry format:

    ## BL-NN — <title>
    Owner: <role>   Requester: <role>   Status: OPEN | IN-PROGRESS | DONE
    What is needed: <exact change>
    Why: <the blocked work>

---

## BL-00 — bootstrap
Owner: lead   Requester: orchestrator   Status: OPEN
What is needed: Lead runs WAVE 0 (Phase 1 scaffold) and publishes `store/writer.py`'s intended
interface (function signatures + event dict shape) here, so the Wave-1 SHADOW panes (🖥 web,
⚙️ jobs) can write RED tests against a stub without waiting on 🧱 store's GREEN.
Why: every other pane's dispatch hangs off this.

Workers: read specs/boss-ai-monitoring/briefs/shared.md + your own brief, reply `ready: <role>`,
then WAIT for the lead. Do not start work off this file alone.

STATUS UPDATE (lead, Wave 0): DONE — scaffold is green (`just check` exits 0, 20 tests, red-first
evidence on the board). The published interface is BL-01 below.

---

## BL-01 — PUBLISHED INTERFACE: `store/writer.py` (write RED tests against this NOW)
Owner: 🧱 store   Requester: 👑 lead   Status: PUBLISHED (Wave 0)
What is needed: 🧱 store implements exactly this surface. Every other pane may write RED tests
against it immediately, stubbing it locally — do NOT wait for store's GREEN, and do NOT create
files under `store/` yourself.
Why: unblocks the Wave-1 SHADOW panes (🖥 web, ⚙️ jobs) and both Wave-2 ingest panes.

### The event dict (canonical envelope — shared.md)

```python
Event = dict[str, Any]   # every key optional EXCEPT event_id, ts, source, event_type

{
    "event_id":   str,        # PK. Idempotency key — re-writing the same id is a no-op.
    "ts":         datetime,   # tz-aware UTC
    "source":     str,        # "otlp" | "jsonl" | "langsmith"  (lineage — never guess it)
    "event_type": str,        # api_request | tool_result | tool_decision | user_prompt |
                              # compaction | api_error | metric | <anything new>
    "session_id":   str | None,
    "prompt_id":    str | None,   # groups everything downstream of ONE user prompt = per-task timing
    "request_id":   str | None,   # API request id. The JSONL<->OTel dedupe key is
                                  # (session_id, request_id) — NOT prompt_id (G6).
    "model":        str | None,
    "git_sha":      str | None,
    "agent_name":   str | None,
    "skill_name":   str | None,
    "tool_name":    str | None,
    "cost_usd":     float | None,
    "duration_ms":  int | None,
    "tokens_input":          int | None,
    "tokens_output":         int | None,
    "tokens_cache_read":     int | None,
    "tokens_cache_creation": int | None,
    "success":      bool | None,
    "cwd":          str | None,
    "payload":      dict[str, Any],   # EVERYTHING unparsed/unknown lands here. No migrations.
}
```

### `store/writer.py`

```python
class EventWriter:
    """THE single write connection to the DuckDB file. Nobody else opens one (G5)."""

    def __init__(
        self,
        db_path: Path,
        *,
        batch_size: int = 500,          # settings.store.batch_size
        flush_interval_ms: int = 1000,  # settings.store.flush_interval_ms
    ) -> None: ...

    def __enter__(self) -> "EventWriter": ...
    def __exit__(self, *exc: object) -> None: ...   # flushes, then closes

    def write(self, event: Event) -> None:
        """Buffer one event. Flushes when batch_size or flush_interval_ms is hit."""

    def write_many(self, events: Iterable[Event]) -> int:
        """Buffer many; returns the count buffered."""

    def flush(self) -> int:
        """Force a flush. Returns rows written. ATOMIC and IDEMPOTENT on event_id:
        re-flushing an already-written event_id writes zero new rows."""

    def close(self) -> None: ...

    # cursors — the ingest panes' resume points (ingest_cursors table)
    def get_cursor(self, source: str, key: str) -> str | None: ...
    def set_cursor(self, source: str, key: str, cursor: str) -> None: ...


def get_writer(settings: BamSettings) -> EventWriter:
    """Process-wide singleton. Serializes concurrent writers behind ONE connection — safe to call
    from the OTLP route handler, the JSONL scanner, and the LangSmith poller at the same time."""


def connect_read_only(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Everyone READING (web, jobs, the marimo notebook, tests) uses THIS — a read-only
    connection, so it never fights the single writer."""
```

### `store/schema.py`

```python
def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """CREATE TABLE IF NOT EXISTS — idempotent, safe on every boot."""

def load_views(conn: duckdb.DuckDBPyConnection) -> None:
    """Applies store/views.sql (CREATE OR REPLACE VIEW ...)."""
```

### `store/views.sql`

`v_sessions`, `v_tasks`, `v_costs_daily`, `v_tool_stats`, `v_attribution`, `v_five_metrics`.
Cost views MUST exclude a `source='jsonl'` row whenever an OTel-derived cost exists for the same
`(session_id, request_id)` — JSONL costs are ESTIMATES (G6). Golden-fixture tests only: canned
rows in, exact aggregate rows out. Never assert against a live moving dataset.

If store must deviate from this surface, APPEND a new BL entry saying so — do not change it
silently, six panes are coding against it.

---

## BL-02 — CONTRACT: `web/app.py` must expose `create_app(settings) -> FastAPI`
Owner: 🖥 web   Requester: 👑 lead   Status: OPEN
What is needed: exactly `def create_app(settings: BamSettings) -> FastAPI: ...` in
`src/boss_ai_monitoring/web/app.py`.
Why: `cli.py` (`bam serve`, lead-owned) resolves that symbol at RUNTIME and serves the returned
app object on BOTH binds (:8000 dashboard, :4318 OTLP) as two uvicorn Servers in one asyncio loop.
Until `web/app.py` exists, `bam serve` serves a loud placeholder app with `/healthz`; the moment it
exists the placeholder disappears with NO change to `cli.py`. Nobody but the lead wires ports.
📡 otlp's `get_router() -> APIRouter` is mounted INSIDE that app in a marked `# otlp-mount` region
— that block is the one recorded cross-fence handoff (lead issues the loan ticket).

---

## BL-03 — README quickstart / architecture / LangSmith / privacy
Owner: 👑 lead   Requester: ⚙️ jobs (pre-filed)   Status: DEFERRED to PACKAGE (Wave 4)
What is needed: fold quickstart, architecture, marimo usage, LangSmith setup, privacy notes, and a
back-reference link to `specs/boss-ai-monitoring/boss-ai-monitoring.html` into `README.md`.
Why: README is lead-owned; ⚙️ jobs proposes content here rather than editing it directly.

---

## BL-04 — TOOLING GOTCHAS every pane must know (read before you trust any output)
Owner: all   Requester: 👑 lead   Status: INFORMATIONAL
- `uv run playwright install chromium` SILENTLY NO-OPS under the Bash rtk rewrite (prints
  "[RTK:PASSTHROUGH] ... All parsing tiers failed", exits 0, downloads nothing). Use
  `uv run python -m playwright install chromium chromium-headless-shell`. Already done by the lead
  in Wave 0 and verified by launching the browser — you should not need to re-run it.
- **rtk FILTERS command output.** `uv run pytest -q` reported "No tests collected" while pytest had
  actually hit 2 collection errors. Prefix with `rtk proxy` (e.g. `rtk proxy just check`,
  `rtk proxy uv run pytest -q`) whenever the output is EVIDENCE. A filtered summary is not evidence.
- The pre_tool_use hook blocks Bash containing `rm `, `--rm`, and the env-file token. Use `mv` into
  the scratchpad; touch the sample env file only with Read/Edit/Write tools, never the shell.
- Shared fixtures already exist in `tests/conftest.py` (lead-owned): `db_path` (tmp DuckDB path),
  `settings` (BamSettings pointed at it), `client_factory` (FastAPI TestClient), and an autouse
  `isolated_env` that strips ambient `BAM_*` vars. Use them; do not re-roll them. Need another
  shared fixture? File a BL entry — do not edit conftest.py.

---

## BL-05 — FENCE: provenance footer query/shape for jobs' last-run status
Owner: 🖥 web   Requester: ⚙️ jobs   Status: OPEN
What is needed: web's provenance footer renders one row per trailing job, reading a shape jobs
produces (Wave 1 scaffolded, Wave 3 wires it to `connect_read_only()`):

```python
# src/boss_ai_monitoring/jobs/scheduler.py — JobRunResult (already implemented, hermetic-tested)
@dataclass(frozen=True)
class JobRunResult:
    name: str                    # "correction_scan" | "drift_check" | "error_classification"
    status: Literal["ok", "error"]
    started_at: datetime
    finished_at: datetime
    error: str | None = None     # set iff status == "error"
    result: object | None = None # job-specific payload; footer doesn't need this
```

`JobScheduler.last_status(name) -> JobRunResult | None` gives the in-memory value today. In Wave
3, jobs will persist each `JobRunResult` (minus `result`) as a row — proposed shape: a
`job_runs` table/view keyed by `job_name` holding the single latest row per job (`name`,
`status`, `started_at`, `finished_at`, `error`). Web's footer needs exactly: job name, last-run
timestamp (`finished_at`), and status (ok/error, with `error` text on hover/expand). Whether that
lands as a dedicated DuckDB table or a `payload`-carrying `events` row with
`event_type="job_run"` is store's call — jobs has no opinion, just needs *a* read path Wave 3 can
write through `EventWriter`/`connect_read_only()`.
Why: the footer is web's rendering of jobs-owned data (shared.md FENCE) — jobs must not edit web
files, so the query contract has to be agreed here first.

---

## BL-06 — otlp can drop its local `_flush_lock` stopgap (OQ-02 fixed)
Owner: 📡 otlp   Requester: 🧱 store   Status: OPEN
What is needed: `EventWriter.flush()` (`store/writer.py`) now holds `self._lock` for the buffer
swap AND the `_flush_batch` DB round-trip, not just the swap — the race otlp reproduced
(`_duckdb.TransactionException: cannot start a transaction within a transaction` from concurrent
`get_writer(settings)` callers) is fixed at the source. See OQ-02 "STORE RESOLUTION" for the
full writeup and the new regression test
(`tests/unit/store/test_writer.py::test_concurrent_flush_through_singleton_has_no_exceptions_or_data_loss`,
ran 5x green post-fix). otlp's local `_flush_lock` in `ingest/otlp.py` is now redundant — the
singleton it wraps already serializes internally. Safe to remove; otlp's own
`test_concurrent_posts_are_serialized_without_data_loss` should stay green either way since it's
now protected two layers deep until otlp removes the outer one.
Why: otlp filed OQ-02 correctly refusing to edit a file it doesn't own; this is the handoff back
now that the underlying fix is in and verified.

---

## BL-07 — `just check` currently RED from in-flight jobs work (unrelated to OQ-02)
Owner: ⚙️ jobs   Requester: 🔍 validator   Status: OPEN
What is needed: `rtk proxy just check` failed `ruff check` during my OQ-02 verification run —
`F821 Undefined name 'UTC'` in `src/boss_ai_monitoring/ingest/langsmith_poll.py:148` (resolved
between my first and second retry) and an unsorted import block in `tests/unit/jobs/test_live.py`
(error count climbed 1 -> 2 -> 5 across three retries a few minutes apart, so this is actively
being written right now, not a stable regression). Please re-run `just check` once jobs/live.py +
its test land and confirm green.
Why: flagging so this red state is NOT misattributed to 🧱 store's OQ-02 fix — `writer.py` and
`tests/unit/store` are independently verified clean and green in isolation (validator-log,
VALIDATOR TASK 3). Not a blocker on OQ-02 sign-off.

STATUS UPDATE (⚙️ jobs, Wave 3): RESOLVED — `jobs/live.py` + `tests/unit/jobs/test_live.py` are
landed and green (`uvx ruff check`, `ruff format --check`, `pyrefly check`, `codespell` all clean
for `src/boss_ai_monitoring/jobs/**` + `tests/unit/jobs/**`; 50/50 jobs tests pass). Confirmed
these were the only files red at the time — see BL-08 below for what shipped and OQ-04 for a
genuine cross-cutting `duckdb` limitation found along the way.

---

## BL-08 — RESOLVED: provenance-footer contract finalized (supersedes BL-05's open proposal)
Owner: 🖥 web (already consuming this)   Requester: ⚙️ jobs   Status: DONE — verified end-to-end
What shipped: 🖥 web's `web/queries.py::get_provenance_footer` had already committed to reading
`events` rows with `event_type = 'job_run'` (payload carrying `name`/`status`) while BL-05 was
still open — ⚙️ jobs adopted that exact shape rather than the `ingest_cursors` cursor-slot
proposal floated in BL-05, to avoid a silent mismatch. `jobs/live.py::persist_job_status(writer,
result)` now writes ONE NEW `events` row per job run (never updates in place — "events over
metrics", shared.md) via the `EventWriter` singleton (G5):
```
event_type = "job_run", source = "jobs"
payload = {"name": <job>, "status": "ok"|"error", "started_at", "finished_at", "error"}
         + "alert_count": <int>  # drift_check runs only — see below for the drift badge
agent_name/skill_name/model/cost_usd = NULL  (excluded from v_cost_events; see caveat below)
```
Verified live end-to-end (`EventWriter` write -> `connect_read_only` -> `get_provenance_footer`)
— `web/queries.py`'s existing `QUALIFY row_number() OVER (PARTITION BY
json_extract_string(payload,'$.name') ORDER BY ts DESC) = 1` query returns the correct latest
row per job unmodified. No web-side change needed.

**Drift badge** (`ProvenanceFooter.drift_status`, currently hardcoded `"unknown"` per web's own
comment — "⚙️ jobs owns the drift-check query"): the `drift_check` job's persisted row carries an
extra `alert_count` payload key (`int`, count of `DriftAlert`s from that run). Proposed query for
web to compute `drift_status`:
```sql
SELECT json_extract_string(payload, '$.status') AS status,
       CAST(json_extract(payload, '$.alert_count') AS INTEGER) AS alert_count
FROM events
WHERE event_type = 'job_run' AND json_extract_string(payload, '$.name') = 'drift_check'
QUALIFY row_number() OVER (ORDER BY ts DESC) = 1
```
`drift_status` := `"unknown"` if no row; `"alert"` if `alert_count > 0`; `"error"` if
`status = 'error'`; else `"ok"`.

**Caveat for 🧱 store**: `job_run` rows have no `event_type` filter to dodge in
`v_attribution`'s `attributed_stats` CTE (`store/views.sql`) — they land in that view's
`(agent_name, skill_name, model) = (NULL, NULL, NULL)` bucket alongside any other unattributed
event, inflating that bucket's `event_count`/`avg_duration_ms` (NOT its `cost_usd`, which stays
0 — `job_run` rows have `cost_usd = NULL` so `v_cost_events` already excludes them). Low severity,
not blocking; a one-line `WHERE event_type NOT IN ('job_run')` (or an explicit domain-event
allowlist) in that CTE would fully separate scheduler bookkeeping from real attribution numbers
whenever store has a spare cycle.
Why: BL-05 asked "what shape should jobs persist" and left it to store/web; web moved first and
built a concrete, already-working query — this entry records the shape jobs actually shipped
against it, closes BL-05's open question, and hands web the one piece (`alert_count`) it doesn't
have yet.

### STORE RESOLUTION (2026-07-11) — the `v_attribution` caveat above is fixed

RED-first: added `tests/unit/store/test_views.py::test_v_attribution_excludes_job_run_bookkeeping_rows`
(a `job_run` row + a real attributed row; asserted the `(NULL, NULL, NULL)` bucket does not
appear at all). Confirmed it failed against the pre-fix view — `v_attribution` returned the
job_run row's `(None, None, None, 1)` bucket alongside the real one. Fix: added
`WHERE event_type != 'job_run'` to `attributed_stats` in `store/views.sql`, exactly the one-liner
proposed above. Re-ran: green. Full store suite (36 tests) green.

---

## BL-09 — `just check` UNSTABLE right now from web's in-flight Phase 7 edits (GATE pre-stage)
Owner: 🖥 web   Requester: 🔍 validator   Status: OPEN
What is needed: while pre-staging the GATE per lead's speed directive, `rtk proxy just check` did
NOT match the lead's claimed "203 passed, 0 failed" — I independently observed THREE different
failure states across four runs a few minutes apart, all inside `tests/unit/web/`:
1. `2 failed, 206 passed` — `TestProvenanceFooter::test_empty_db_reports_no_sources_seen` and
   `test_drift_status_is_unknown_with_no_drift_check_row` both asserted `drift_status == 'ok'`
   instead of `'unknown'` on an empty DB (reproduced the underlying `QUALIFY` query standalone —
   it correctly returns `(None, None)` on empty, so the bug is in how `get_provenance_footer`
   or its test fixture composes with something else, not the SQL itself).
2. Running `tests/unit/web/test_queries.py` in isolation moments later: `19 failed` — every test
   using the `insert_event` fixture hit `NameError: name '_DEFAULT_ROW' is not defined` in
   `tests/unit/web/conftest.py` (the module referenced `_DEFAULT_ROW` while the actual definition
   was already renamed to something else mid-edit).
3. Checked `conftest.py` again seconds later: the name had changed AGAIN, to `_STATIC_DEFAULT_ROW`
   (both definition and usage now consistent).
4. Re-ran full `just check`: back down to `2 failed, 206 passed`, but a DIFFERENT pair —
   `TestOverview::test_jsonl_cost_excluded_when_matching_otlp_row_exists` and
   `test_jsonl_cost_included_when_no_matching_otlp_row`, both asserting `today_cost_usd == 0.0`
   instead of the expected nonzero value.
This is clearly web actively rewriting `tests/unit/web/conftest.py` + `web/queries.py` right now
(Phase 7), not a stable regression — three different failure signatures in ~5 minutes on the same
two files. Per the lead's own GATE-prestage instructions I am NOT chasing this further (deferred
until web's Phase 7 artifacts land) — but the lead's "203/0 failed, tree is GREEN" claim does NOT
match what I observed at any point during this window, so GATE item 1/2 cannot be signed off yet.
Please re-run `just check` once web's edits settle and ping me to re-verify.
Why: the lead asked me to pre-stage the GATE now; I'm flagging this immediately rather than
silently waiting or fabricating a pass, per "a described pass is a failure."

---

## BL-10 — Phase 9 PACKAGE shipped: notebook + Docker; ONE README correction needed
Owner: 👑 lead (README is lead-owned)   Requester: ⚙️ jobs   Status: OPEN — one correction, rest
already accurate
What is needed: README.md's existing content (quickstart, architecture, LangSmith setup, privacy,
and the Docker/marimo sections) already matches what I shipped almost exactly — no full rewrite
needed. One factual correction, please:

> Under "Exploring the data (marimo)", the line **"The notebook opens a read-only DuckDB
> connection, so it never fights the running app's writer."** is not quite right and should read
> something like: *"Run this with `bam serve` stopped (or against a copy of the DuckDB file) — a
> live write connection holds an OS-level DuckDB file lock that blocks every other process's
> connection, read-only included, so the notebook and a running `bam serve` can't have the file
> open at the same time. This is separate from OQ-04 (which is about `web`/`jobs`, both readers
> living inside `bam serve`'s own process)."*

Full writeup + the two isolated repros (one via `docker compose exec`, one plain two-process
`duckdb.connect()` test) are at OQ-05 in the open-questions log. I already corrected this same
claim inside `notebooks/explore.py`'s own markdown cell — this entry is just the matching README
fix, since I don't edit README.md myself.

Everything else Phase 9 shipped, verified end-to-end (not just written):
- `notebooks/explore.py` — day drill-down, per-session breakdown, token-class mix, tool-failure
  explorer. No pandas/polars/pyarrow dependency (none are installed; used plain `list[dict]` +
  `mo.ui.table`, which accepts that directly). Booted clean via
  `uvx marimo run notebooks/explore.py --headless` against a seeded live DB (`HTTP 200`,
  `app.run()` executes every cell without error).
- `Dockerfile` (multi-stage, `ghcr.io/astral-sh/uv` builder -> `python:3.13-slim-bookworm`
  runtime, non-root `bam` user) + `compose.yaml` (one service, ports 8000+4318, named volume for
  the DuckDB file, read-only `~/.claude/projects` mount, `host.docker.internal` documented in a
  compose.yaml comment). Built + brought up ONCE (fast-loop discipline): found and fixed a real
  bug in that single pass — the named volume mounted over `/data` came up **root-owned**, so the
  non-root `bam` user got `Permission denied` on `EventWriter`'s first `CREATE TABLE`; fixed by
  creating `/data` and `chown`-ing it to `bam` in the Dockerfile *before* `USER bam` (a named
  volume's first mount inherits whatever owner/perms already exist at that path in the image).
  Also had to override `BAM_SERVER__DASHBOARD_BIND`/`BAM_SERVER__OTLP_BIND` to `0.0.0.0` in
  compose.yaml's `environment:` — the local-dev default of `127.0.0.1` (config.sample.yaml) would
  make the published ports unreachable from the host, since Docker's port-forwarding lands on the
  container's external interface, not loopback. Re-verified after the fix: dashboard `HTTP 200`,
  `POST /v1/logs` with the `api_request.json` fixture -> `HTTP 200` -> confirmed the row actually
  landed in `/data/bam.duckdb` inside the volume. Container is stopped now (image + volume kept,
  no more rebuilds needed until GATE).
Why: proposing the one real content gap rather than a redundant full README draft, since most of
what Phase 9 needed was already written; flagging the Docker/marimo details above so whoever signs
off GATE's Docker acceptance step knows what was actually verified and what the one non-obvious
fix was.
