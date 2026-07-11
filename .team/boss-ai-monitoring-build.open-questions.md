# Open Questions — boss-ai-monitoring-build

APPEND-ONLY. File one THE MOMENT you are stuck; `cmux notify` on first open.
An open question is a SUCCESS, not a failure. If you catch yourself typing
"presumably / it appears / likely / should be" — that is an OQ, not a sentence.

Block format:

    ## OQ-NN — <title>
    Status: OPEN | ANSWERED | NEEDS-HUMAN
    Spec: <section / brief / HTML anchor>
    What I tried:
    Why it is stuck:
    My best guess:
    Cost of guessing wrong:

Two RISKs the spec names in advance — an OQ here is expected, not a defect:
  - JSONL format drift across Claude Code versions.
  - The LangSmith `thread_id` ↔ `session.id` best-effort join.

---

## OQ-01 — Stop hook blocks session end on repo-wide pyrefly, not pane-scoped
Status: ANSWERED — human decision, relayed by the orchestrator. See LEAD TRIAGE at the end of this block.
Spec: tooling/harness — `uv run pyrefly check --baseline pyrefly-baseline.json src tests tools
.claude/status_lines/status_line_v10.py` runs as a Stop hook with `exit 2` on any error.
What I tried: confirmed my own scope (`src/boss_ai_monitoring/jobs/**`,
`tests/unit/jobs/**`) is 0 errors against the same baseline
(`uv run pyrefly check --baseline pyrefly-baseline.json src/boss_ai_monitoring/jobs
tests/unit/jobs`). The 13 recurring errors are all in `store/` and `web/` files (e.g.
`store/writer.py`, `web/app.py`, `tests/unit/web/test_queries.py`) — panes still in-flight per
the Wave 1 SHADOW dispatch.
Why it is stuck: the hook checks the whole `src tests` tree every Stop, so it re-fires
identically every turn regardless of what I do, since it's gated on other panes' WIP files I
don't own and can't edit (exclusive file ownership). I have nothing further to act on — I'm
idle at `ready: jobs`, but the hook keeps blocking Stop as if I were the one leaving errors.
My best guess: this is expected to self-resolve once 🧱 store and 🖥 web each reach their own
GREEN — the baseline file may need lead/human review to see if it should exclude
pane-in-flight paths during a multi-pane run, or the hook should scope to changed files only.
Cost of guessing wrong: low if I just wait; high if this is meant to signal "something is
actually broken" and gets silently ignored because every pane sees the same noise.

### LEAD TRIAGE (2026-07-11) — ANSWERED. ⚙️ jobs was right to file this; it was a real hazard.

The failure mode ⚙️ jobs spotted is worse than an annoyance: a repo-wide Stop hook that
force-continues an IDLE pane on OTHER panes' work-in-progress type errors is a direct threat to
exclusive file ownership — a pane that cannot stop starts "helpfully" editing files it does not own.

RESOLUTION (human-authorized, orchestrator-executed): the Stop hook is NEUTRALIZED for the
duration of this run. The orchestrator removed ONLY `.hooks.Stop` from `.claude/settings.json`;
every other hook is untouched; the verbatim original is backed up and WILL be restored before GATE.
`.claude/settings.json` is ORCHESTRATOR-OWNED — no pane, including the lead, edits it.

THE DEFINITION OF DONE IS UNCHANGED. pyrefly is still fully enforced inside `just check` and inside
`.github/workflows/ci.yml`, and GATE still requires a green `just check`. Nothing was relaxed; the
gate simply moved back to where it belongs — the gate — instead of firing at every pane's turn end.

THE ERRORS ARE REAL AND ARE STILL OWNED. Lead independently reproduced them with
`rtk proxy uv run pyrefly check` (not the baseline invocation, so counts differ slightly). Two
classes, both owner-fixable, neither of which anyone else may touch:

- **🧱 store** — `duckdb`'s `.fetchone()` is typed `Optional[tuple]` and is being subscripted
  directly. 2 in `src/boss_ai_monitoring/store/writer.py` (117, 122) + 7 in
  `tests/unit/store/test_writer.py` (30, 44, 147, 148, 149, 150, 177). Fix: a None guard or an
  `assert row is not None` before indexing — not a `# type: ignore`.
- **🖥 web** — same `.fetchone()` pattern in `tests/unit/web/test_queries.py`, plus
  `tests/unit/web/{test_app,test_queries}.py` importing a bare `conftest` module
  (`Cannot find module 'conftest'`) — import the fixtures through pytest, not by importing conftest.

Both are part of reaching each pane's own GREEN. Dispatched to store and web on 2026-07-11.

⚙️ jobs: you are unblocked and were never the cause. Stand by for your Wave-3 dispatch.

---

## OQ-02 — `EventWriter.flush()` not thread-safe across concurrent callers on the singleton
Status: OPEN
Spec: BL-01 `store/writer.py` — `get_writer()` docstring: "safe to call from the OTLP route
handler, the JSONL scanner, and the LangSmith poller at the same time."
What I tried: otlp.md's required edge case "concurrent posts (writer serialization holds)" —
hit `/v1/logs` with 6 concurrent threads sharing one `get_writer(settings)` singleton (real repro
in `tests/unit/ingest/test_otlp.py::TestLogsEndpoint::test_concurrent_posts_are_serialized_without_data_loss`,
now green). Without a mitigation it reliably raised
`_duckdb.TransactionException: cannot start a transaction within a transaction`.
Why it is stuck: `EventWriter.flush()` (store/writer.py:98-106) only holds `self._lock` around
swapping `self._buffer` out; it releases the lock *before* calling `self._flush_batch`, which is
where `BEGIN TRANSACTION` / `DELETE` / `INSERT` / `COMMIT` actually run against the one shared
connection. Two threads that each reach `flush()` at the same time can both pass the buffer-swap
under the lock and then both call `_flush_batch` unsynchronized, racing on `BEGIN TRANSACTION`.
This isn't otlp-specific — any two producers sharing the `get_writer()` singleton (otlp route
handler, jsonl scanner, langsmith poller) can hit it once more than one is live.
My best guess: `_flush_batch` needs to run inside the same `with self._lock:` block that
protects the buffer swap in `flush()`, not after it releases — the buffer swap alone isn't the
thing that needs serializing, the whole DB round-trip is. This is `store`'s file
(`src/boss_ai_monitoring/store/writer.py`) — I don't edit it (exclusive ownership); I've added a
serialization lock local to `ingest/otlp.py` (`_flush_lock`) so my own router never triggers this
race, but that only protects otlp-vs-otlp concurrency, not otlp-vs-jsonl or otlp-vs-langsmith once
those panes are live and sharing the same writer singleton.
Cost of guessing wrong: low today (only otlp calls `get_writer` so far, and I've locally
mitigated); becomes a real intermittent-failure risk once jsonl (Wave 2) and langsmith (Wave 3)
both call `get_writer(settings)` concurrently with otlp under real traffic.

### STORE RESOLUTION (2026-07-11) — FIXED

Confirmed the race exactly as diagnosed: RED-first, added
`tests/unit/store/test_writer.py::test_concurrent_flush_through_singleton_has_no_exceptions_or_data_loss`
(8 threads x 25 events, hammering `flush()` after every `write()` through one `get_writer(settings)`
singleton). Ran it 3x against the pre-fix code — reliably raised
`TransactionException: cannot start a transaction within a transaction` (+ cascading "transaction is
aborted" on every subsequent call) every time.

Fix: `EventWriter.flush()` (`store/writer.py`) now holds `self._lock` for the buffer swap AND the
`_flush_batch` DB round-trip (`BEGIN`/`DELETE`/`INSERT`/`COMMIT`) — the whole thing runs inside one
`with self._lock:` block instead of releasing before the transaction starts. `write()`/`write_many()`
already released the lock before calling `self.flush()`, so this doesn't introduce reentrant-lock
deadlock. Re-ran the new test 5x post-fix: green every time. Full store suite (31 tests) and repo-wide
`rtk proxy just check` (173 tests, ruff/format/pyrefly/codespell all clean) both green.

`get_writer()`'s singleton is now actually safe for concurrent callers as its docstring promises —
otlp, jsonl, and langsmith can share it. Filed BL-05 telling 📡 otlp it can drop the local
`_flush_lock` stopgap in `ingest/otlp.py` now that the underlying writer serializes correctly.

---

## OQ-03 — otlp fixtures built from published docs, not a captured live session
Status: OPEN (named RISK #1 from shared.md — filing this is expected, not a defect)
Spec: otlp.md ¶1 "Fixtures first: capture real payloads by running Claude Code once with
telemetry pointed at a dump script; commit sanitized fixtures."
What I tried: I did not spin up a second, telemetry-enabled Claude Code session from inside this
build-team pane to capture a live OTLP export (recursive/self-referential, and outside this pane's
sandboxed non-interactive scope). Instead I fetched `code.claude.com/docs/en/monitoring-usage`
directly and built `tests/fixtures/otlp/{api_request,tool_result,tool_decision,user_prompt,
compaction,api_error,metrics_export}.json` from the documented `event.name`/attribute schema
verbatim (event names, attribute keys, the `resourceLogs -> scopeLogs -> logRecords` /
`resourceMetrics -> scopeMetrics -> metrics -> dataPoints` shapes, proto3-JSON `AnyValue` typing).
Why it is stuck: I have no live Claude Code OTLP traffic to diff these fixtures against inside
this run.
My best guess: the docs are authoritative and current, so the schema (attribute names, event
names, resource- vs record-level attribute placement) should match real traffic closely. The
named risk is real either way: real Claude Code telemetry could still add/rename fields between
versions. My parser is defensive by design specifically for this reason — unmapped attributes
land in `payload` (no crash, no drop), unknown `event.name` values are stored as-is as
`event_type`, and coercion failures fall back to the raw value instead of raising. Malformed input
still returns 400 without touching the writer.
Cost of guessing wrong: low for MVP (defensive parsing absorbs drift, nothing gets dropped) but
the `api_request.json` fixture is GATE-curled verbatim per otlp.md, so if real attribute names
differ from the docs (e.g. an actual field turns out to be `session_id` flat instead of
`session.id`, or `cost.usd` instead of `cost_usd`), the mapping table in `ingest/otlp.py`
(`_ATTR_TO_COLUMN`) needs a one-line update, not a redesign. Recommend the validator's manual E2E
step (otlp.md Acceptance: `just dev` + one real Claude Code prompt, then
`duckdb "$(uv run bam config db-path)" "SELECT event_type, count(*) FROM events GROUP BY 1"`)
double as the real-payload diff against these fixtures once the router is mounted (BL-02).

---

## OQ-04 — `connect_read_only()` cannot coexist with a live `get_writer()` in the same process
Status: NEEDS-STORE (this is load-bearing — it blocks jobs' and web's live read paths, not a
nice-to-have)
Spec: BL-01 `store/writer.py` — `connect_read_only(db_path) -> duckdb.DuckDBPyConnection`:
"Everyone READING (web, jobs, the marimo notebook, tests) uses THIS — a read-only connection, so
it never fights the single writer." BL-02: `bam serve` runs ONE FastAPI app on both binds in ONE
asyncio loop/process; jobs' scheduler is a background asyncio task in that same process/loop.
What I tried: reproduced directly with raw `duckdb` (no jobs/store code involved, isolating this
from my own logic):
```python
import duckdb
w = duckdb.connect(path)                    # a live, open, non-read-only connection
w.execute("CREATE TABLE t(x INT)")
duckdb.connect(path, read_only=True)         # <-- raises, even though w has no open transaction
# _duckdb.ConnectionException: Connection Error: Can't open a connection to same database
# file with a different configuration than existing connections
```
Also reproduced with the real functions: `get_writer(settings)` (leaves its `EventWriter._conn`
open, as a process-wide singleton is supposed to) followed by `connect_read_only(settings.store
.db_path)` in the same process raises identically — every one of my `tests/unit/jobs/test_live.py`
tests hit this until I rewrote them to use short-lived `EventWriter(db_path)` context managers
(fully closed before the read) instead of the `get_writer()` singleton, which sidesteps it in
*tests* but does not reflect how `bam serve` actually runs.
Why it is stuck: `get_writer()`'s whole purpose is a connection that stays open for the app's
lifetime (never closed between operations) — the instant any OTLP/JSONL/LangSmith event has
been ingested and the writer singleton is live, EVERY subsequent `connect_read_only()` call
anywhere in that same process (web routes, jobs' scheduler runs, the marimo notebook if run
in-process) will raise this `ConnectionException`. This is `duckdb`'s in-process connection-cache
behavior, not a bug in anyone's code — but the documented `connect_read_only()` contract assumes
it's safe to call concurrently with a live writer, and duckdb 1.5.4 does not allow that.
My best guess: two known duckdb-compatible fixes, both store's call since both touch
`store/writer.py`:
  1. Have readers pull a `.cursor()` off the SAME live connection instead of opening an
     independent `duckdb.connect(..., read_only=True)` — duckdb cursors on one connection object
     support concurrent queries via MVCC without the config-mismatch check. This likely means
     `connect_read_only(db_path)` needs to become writer-aware (e.g. reuse `get_writer`'s
     connection via `.cursor()` when a writer for that path is already live in-process, and fall
     back to a fresh `read_only=True` connect only when no writer is live — e.g. in tests, the
     marimo notebook run standalone, or store's own golden-fixture tests).
  2. Alternatively just document/enforce single-process discipline differently (a wholly separate
     DuckDB `ATTACH ... (READ_ONLY)` inside an in-memory root connection) — more invasive, likely
     not worth it if (1) works.
Cost of guessing wrong: HIGH — if unresolved, `bam serve` will 500/crash the first time a web
route or a jobs run tries to read while the OTLP writer has ever been touched, which is every
real run past the first ingested event. I did not attempt a fix myself (`store/writer.py` is not
mine); my own `jobs/live.py` functions (`fetch_events`, `run_correction_scan`, `run_drift_check`,
`run_error_classification`, `persist_job_status`, `read_job_status`) are otherwise correct and
fully green against a closed-writer-then-read pattern — they need no changes once
`connect_read_only()` itself is fixed; only my test setup had to route around this to prove the
job logic. `build_scheduler(settings)`-style single-call wiring (writer + live callables +
scheduler) is deliberately NOT shipped this wave because I cannot honestly claim it works
end-to-end until this is resolved — see BL-07 for the composable pieces I did ship.

---

## OQ-jsonl-01 — JSONL reverse-engineering assumptions (RISK #1), verified against real transcripts
Status: ANSWERED (self) — defensive-by-design, verified via manual E2E against real data
Spec: jsonl-langsmith.md Phase 4 ("JSONL format is reverse-engineered" — shared.md RISK #1)
What I tried: inspected ~10k real lines across `~/.claude/projects/**/*.jsonl` (this machine) before
writing `ingest/jsonl.py`, specifically hunting for: a `git_sha`-equivalent field (checked
`gitSha`/`git_sha`/`gitCommit`/`commitSha` at top level) — never observed, in any file; how
`promptId` is threaded (assistant lines NEVER carry it directly — only `type: "user"` lines do,
both the initiating prompt and every `tool_result`, and it repeats identically across a whole
task's user-type lines); the `isCompactSummary: true` marker for compaction boundaries; `tool_use`/
`tool_result` block shapes for tool_name resolution.
Why it is stuck: no published schema exists to verify against; Claude Code versions could add/
rename/remove any of this at any time (the named risk).
My best guess / what shipped: `git_sha` extraction checks the plausible field names defensively
and returns `None` when absent (currently always) — a future version emitting one needs zero code
change here. `prompt_id` is threaded forward per-session (most-recent `promptId` seen on a
user-type line applies to every line after it until the next one) — verified accurate against real
multi-turn sessions where the same `promptId` reappears on every tool_result within one task.
`tool_decision` has no JSONL structural equivalent (that's OTel/hook-only) so `jsonl.py` does not
emit it — confirmed in scope by the dispatch's own phrasing ("assistant/user/tool entries").
Real-data verification (manual E2E, `~/.claude/projects`, 1944 files): 107,590 events ingested
zero errors/crashes (68,652 api_request / 34,621 tool_result / 4,316 user_prompt / 1 compaction);
re-scan correctly ingested ONLY the delta from still-growing sessions (133 new events across the
scan's own 146s window — i.e. sessions that grew *while the first scan was running*, which is
exactly the gap-fill behavior working, not a bug).
Cost of guessing wrong: low — every parse path is defensive (unmapped fields land in `payload`,
malformed lines skip-and-log per line, never crash the scan), so a wrong assumption degrades to
"less accurate grouping," never data loss or a crash.

## OQ-jsonl-02 — LangSmith thread_id<->session_id join (RISK #2): confirms OQ-04, real rate-limit finding
Status: ANSWERED (self) — join verified against LIVE data; confirms OQ-04's `connect_read_only()`
finding independently; shipped a real-world rate-limit mitigation
Spec: jsonl-langsmith.md Phase 5; shared.md RISK #2; OQ-04 (🧱 store, NEEDS-STORE)
What I tried: `poll_once()` needs the set of locally-known `session_id`s to decide match vs
unmatched. Calling `connect_read_only(db_path)` while the process's `EventWriter` (from
`get_writer(settings)`) is also live raised the EXACT same
`_duckdb.ConnectionException: Can't open a connection to same database file with a different
configuration than existing connections` that OQ-04 already documents in depth. I did not touch
`store/writer.py` (not mine) — worked around it locally in `ingest/langsmith_poll.py` by reading
through the writer's OWN connection (`writer._conn.execute(...)`) instead of opening a second one,
which is exactly OQ-04's proposed fix option (1) ("readers pull a `.cursor()` off the SAME live
connection"), just applied ad hoc at the call site rather than inside `connect_read_only()` itself.
Separately: manual E2E against the REAL LangSmith API (ambient key; verified
`$CC_LANGSMITH_PROJECT == $LANGSMITH_PROJECT` beforehand) — first attempt (unbounded "since epoch"
first-poll, no result cap) exhausted all 6 retries (~126s of backoff) and returned
`status="error"` cleanly (no crash, exactly as designed) instead of ever succeeding. Root cause:
this project is receiving real trace writes multiple times per SECOND right now (this very
multi-pane build session is actively tracing to it), and the SDK paginates with one HTTP request
per page — an unbounded listing over that volume blew the ~10 req/10s budget before my retry loop
even helped.
My best guess / what shipped: (a) the first-ever poll for a project now looks back 7 days instead
of "since 1970" — matches shared.md's own "<=7-day windows" rate-limit guidance; (b) added a
`limit` param (default 200) threaded into `list_runs(..., limit=...)` to bound requests-per-poll —
anything left over is picked up by the next poll via the persisted cursor, nothing is lost, just
spread out. Re-ran with `limit=50`: SUCCESS — 50 real runs ingested, ALL 50 matched an existing
local `session_id` via `thread_id` (100% match rate on live data, not simulated), and 6 real
sessions now carry BOTH `source='jsonl'` and `source='langsmith'` rows sharing one `session_id`
(one of them is this very build session's own session_id) — the Phase 5 E2E acceptance criterion,
genuinely satisfied against live traffic.
Cost of guessing wrong on the join itself: low — an unmatched run lands with `session_id = NULL`
and its `thread_id` preserved verbatim in `payload` (unit-tested), never silently merged; the
"LangSmith-only" bucket is queryable as `source='langsmith' AND session_id IS NULL` for whoever
builds that view. Cost of guessing wrong on rate-limit sizing: low — `limit`/lookback are both
plain keyword args on `poll_once`, one-line tuning if 200/7d ever proves wrong for a given project.

## OQ-jsonl-03 — OQ-04 (`connect_read_only()` vs live writer) is ALREADY breaking `just check` at HEAD
Status: OPEN — informational, not mine to fix; flagging blast radius for 🧱 store / 🖥 web / lead
Spec: OQ-04 (still NEEDS-STORE as of this writing)
What I tried: ran full `just check` after finishing my own ticket (unrelated to my changes — my own
`src/boss_ai_monitoring/ingest/**` + `tests/unit/ingest/**` are fully green in isolation and in the
full suite). `just check`'s `test` step currently fails:
`tests/e2e/test_dashboard.py::test_live_feed_shows_a_posted_otlp_event_via_sse` — same
`_duckdb.ConnectionException` as OQ-04, raised from `web/app.py:62` (`_poll_events` ->
`connect_read_only(settings.store.db_path)`) while a writer is live in the same process.
Reproduced in isolation (single-test run), so it is stable, not test-order flake.
Why it is stuck: not my file (`web/app.py` is 🖥 web's), and the root-cause fix belongs in
`store/writer.py` per OQ-04's own analysis — I have nothing further to add technically, just
confirming OQ-04's predicted blast radius ("`bam serve` will 500/crash the first time a web route
... tries to read while the OTLP writer has ever been touched") is no longer theoretical.
My best guess: this blocks GATE (`just check` must be green) until OQ-04 lands, independent of any
per-pane GREEN.
Cost of guessing wrong: none — purely a status report, not a design decision.

---
(end of current questions)
