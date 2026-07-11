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
(end of current questions)
