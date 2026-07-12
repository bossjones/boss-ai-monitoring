# boss-ai-monitoring-build — BOARD (durable state)

> Any restarted or confused agent: **read this file and resume from the recorded state.**
> Lead-owned. Workers append to `.backlog.md` / `.open-questions.md` instead of editing this.

## FSM

```
SCAFFOLD ✅ -> STORE-CORE ▶ -> INGEST-FANOUT -> INTEGRATE -> PACKAGE -> GATE -> DONE
```

**Current state: GATE** — all 9 phases implemented, `just check` green (208 tests, pyrefly 0
errors), INTEGRATE+PACKAGE committed (`62d5137`). ✅ validator is running the 8-point checklist.

INGEST-FANOUT closed 2026-07-11: 📡 otlp Phase 3 **independently validator-verified** (raw output
in `.validator-log.md` — red-first proven by gutting the handler, 12/24 failed; all edge cases
proven by direct DB query, not response-code inference), 🧱 store's OQ-02 race **fixed red-first**.
Committed `b4a47a2`. Gate: `just check` green, pyrefly 0 errors, **173 tests**.

## Roster

| pane | role | phase(s) | state |
|---|---|---|---|
| 👑 lead | scaffold, config, cli, gate files, board | 1 | 🟢 SCAFFOLD done |
| 🧱 store | schema.py, writer.py, views.sql | 2 | 🟢 GREEN, committed `fa1f7e5` (tests+30, red-first-Y) |
| 📡 otlp | ingest/otlp.py | 3 | 🟢 DONE — validator-verified; BL-06 stopgap removed (`30cb2b0`). Filed OQ-02 + OQ-03 |
| 📜 jsonl | ingest/jsonl.py, ingest/langsmith_poll.py | 4+5 | 🟢 DONE (`ad25e17`) — LIVE LangSmith read-back: 50 runs, 100% session match. Confirmed OQ-04 |
| 🖥 web | web/**, e2e, docs/AGENT_LOOP.md | 6+7 | 🟢 DONE (tests+47, red-first-Y) — LT-01 closed |
| ⚙️ jobs | jobs/**, notebook, Dockerfile, compose | 8+9 | 🟢 DONE — Phase 8 (`61ede57`, tests+50) + Phase 9 (docker, marimo) |
| ✅ validator | validator-log.md, GATE checklist | GATE | 🔵 running the 8-point GATE. Verified: store, otlp, OQ-02 fix, jsonl |

**Independent verifications on record** (raw output in `.validator-log.md` — none of these are a
pane's self-report):
- 🧱 store Phase 2 + the OQ-02 concurrency fix (red-first reproduced 5/5; idempotency proven under
  cross-thread collisions).
- 📡 otlp Phase 3 (gutted handler -> 12/24 fail; every edge case proven by direct DB query, not
  response-code inference; `request_id` confirmed populated).
- 📜 jsonl Phases 4+5 (red-first on `parse_line` 9/16 and `poll_once` 11/15; all four transcript
  shapes; malformed-line skip-and-log; cursor delta + truncation safety; **G6 dedupe proven through
  the real views against a golden fixture — cost came back 5.75, not 10.6, so the JSONL estimate is
  genuinely excluded when an OTel cost exists**; live LangSmith cross-check matched the claimed
  50 runs / 100% across two independent sources).

**Also open:** 🧱 store is fixing OQ-02 (the writer race) — see Open questions below. It is GREEN
otherwise; the race is a defect against its own published contract, not a regression of Phase 2.

**Gate state:** `just check` green at **172 tests** (lead's run). One lead-owned fix on the way:
codespell flagged "iTerm" inside otlp's captured fixtures — a false positive on a real terminal
name, so it was added to `ignore-words-list` in pyproject rather than corrupting a fixture to
appease a spellchecker.

## STORE-CORE gate — `just check`, green (lead's own run, not store's claim)

```
uv run pyrefly check
 INFO 0 errors (1 warning not shown)
uv run pytest -q
121 passed, 1 warning in 0.91s
```

The 13 OQ-01 pyrefly errors (9 store, 4 web) are GONE. ✅ validator is independently confirming
they were fixed with real None guards and not papered over with a `type: ignore`, a widened
pyrefly baseline, or a lint exclusion — and is proving RED-FIRST by gutting store's implementation
and checking the tests actually fail. Its verdict lands in `.validator-log.md`.

> LEAD NOTE: Wave 2 was dispatched in PARALLEL with that audit rather than serialized behind it —
> store's GREEN is confirmed by the lead's own `just check`, and otlp/jsonl touch disjoint files.
> If the audit finds a red-first violation, 🧱 store fixes it without blocking the ingest panes.

## Wave plan

- **WAVE 0 (lead)** — Phase 1 scaffold, config, cli, `just check` baseline. **DONE**
- **WAVE 1** — 🧱 store Phase 2 (critical path). SHADOW: 🖥 web + ⚙️ jobs scaffold hermetically
  against fixtures and the published writer interface (no live DB).
- **WAVE 2** — 📡 otlp Phase 3 + 📜 jsonl Phase 4+5, once store is GREEN.
- **WAVE 3** — 🖥 web Phase 6+7 + ⚙️ jobs Phase 8 wired to live events.
- **WAVE 4** — ⚙️ jobs Phase 9 (marimo, Dockerfile, compose).
- **GATE** — ✅ validator personally re-runs the 8-point checklist in `briefs/validator.md` and
  pastes RAW output. A described pass is a failure.

## Phase-boundary commits (local only — NEVER push)

| phase | commit | status |
|---|---|---|
| SCAFFOLD baseline | `2994737 feat(scaffold): uv package, config, bam CLI, TDD harness` | ✅ committed |
| STORE-CORE green | `fa1f7e5 feat(store): DuckDB schema, batched writer, SQL views; web shadow scaffold` | ✅ committed |
| INGEST-FANOUT green | `b4a47a2 feat(ingest): OTLP receiver, JSONL reader, LangSmith poller; fix writer race` | ✅ committed |
| INTEGRATE green | `62d5137 feat(web,jobs): live dashboard, agent loop, docker, marimo` | ✅ committed |
| PACKAGE green | (same commit `62d5137`) | ✅ committed |
| GATE clean | — | pending |

## WAVE 0 baseline — `just check`, green (2026-07-11)

```
uv run ruff check .
All checks passed!
uv run ruff format --check .
19 files already formatted
uv run pyrefly check
 INFO Checking project configured at `/Users/bossjones/dev/bossjones/boss-ai-monitoring/pyproject.toml`
 INFO 0 errors (1 warning not shown)
uv run codespell
uv run pytest -q
....................                                                     [100%]
20 passed in 0.12s
```

RED-FIRST evidence (each failed before its implementation existed):

```
tests/test_smoke.py::test_package_imports
  E  AttributeError: module 'boss_ai_monitoring' has no attribute '__version__'   -> 1 failed
tests/unit/test_config.py
  E  ModuleNotFoundError: No module named 'boss_ai_monitoring.config'             -> collection error
tests/unit/test_cli.py
  E  ImportError: cannot import name 'cli' from 'boss_ai_monitoring'              -> collection error
tests/unit/test_config.py (flat-alias round, added after config.py existed)
  FAILED test_flat_bam_db_path_alias_is_honored
  FAILED test_flat_claude_projects_dir_alias_is_honored
  FAILED test_empty_flat_alias_is_ignored                                          -> 3 failed, 10 passed
```

CLI verified against the real binaries (not described — run):

```
$ uv run bam config db-path
/Users/bossjones/.local/share/boss-ai-monitoring/bam.duckdb
$ BAM_DB_PATH=/tmp/bam-check.duckdb uv run bam config db-path
/tmp/bam-check.duckdb
$ duckdb "$(uv run bam config db-path)" "SELECT 'db reachable' AS ok"
│ db reachable │
```

## Decisions and deviations recorded in Wave 0

- **Build backend is hatchling**, per the spec. `uv init` (uv 0.11.14) now defaults to `uv_build`;
  the lead switched it back.
- **Flat env aliases are real.** The prompt requires `BAM_DB_PATH` / `BAM_CLAUDE_PROJECTS_DIR` in
  the sample env file, but G4's scheme spells them `BAM_STORE__DB_PATH` /
  `BAM_INGEST__CLAUDE_PROJECTS_DIR`. Rather than document dead variables, `config.py` honors both:
  the nested form outranks the flat alias, and an empty alias (`BAM_DB_PATH=`, as the sample ships)
  is ignored rather than blanking the path. Tested.
- **`bam serve` placeholder app.** `cli.build_app()` resolves `boss_ai_monitoring.web.app` at
  RUNTIME via importlib (a static import would fail pyrefly while web/app.py does not exist).
  Until 🖥 web lands it serves a loud placeholder app with `/healthz`. **web's contract:
  `create_app(settings: BamSettings) -> FastAPI`, with the otlp router mounted inside.**
- **ruff excludes** `.claude/`, `logs/`, `specs/`, `notebooks/` — human-owned/harness files, not
  package code. (`.claude/status_lines/*.py` fails ruff's import sort; it is not ours to fix.)

## GOTCHAS discovered this run (add yours)

- **`uv run playwright install chromium` silently no-ops** — the Bash rtk rewrite mangles it
  ("[RTK:PASSTHROUGH] playwright parser: All parsing tiers failed", exit 0, nothing downloaded).
  Use **`uv run python -m playwright install chromium chromium-headless-shell`**. Verified by
  actually launching the browser: `Chrome Headless Shell 149.0.7827.55`. Note playwright wants
  BOTH chromium and the headless shell build.
- **rtk filters command output.** `uv run pytest -q` printed "No tests collected" while pytest had
  really hit 2 collection errors. Prefix with `rtk proxy` (e.g. `rtk proxy just check`) whenever
  you need the true output. Do not trust a filtered summary as evidence.
- The pre_tool_use hook blocks Bash containing `rm `, `--rm`, and the env-file token — so the
  sample env file can only be touched with the Read/Edit/Write tools, never via shell.

## Loan tickets

- **LT-01 — `# otlp-mount` region in `src/boss_ai_monitoring/web/app.py`.** Issued to 🖥 web
  2026-07-11 (Wave 3). Scope: the MOUNT CALL ONLY, inside that marked region, for 📡 otlp's
  `get_router() -> APIRouter`. 📡 otlp keeps ownership of the router's internals — web does not
  edit `ingest/otlp.py`. Ticket closes when Phase 6 is green. Nobody but the lead's `cli.py` wires
  ports.

## Open questions

- **OQ-04 — ✅ CLOSED (`1c0c938`). Was a 🚨 SHIP-BLOCKER.** Filed by ⚙️ jobs; **independently
  reproduced by the lead with raw duckdb, zero project code involved.** `connect_read_only(path)`
  CANNOT coexist with a live `get_writer()` connection in the same process — duckdb refuses a
  second connection to the same file with a different configuration while one is open
  (`ConnectionException: Can't open a connection to same database file with a different
  configuration than existing connections`).
  **Why this is severity-1 and not a test nuisance:** `bam serve` is ONE process — web routes, the
  mounted OTLP router, and jobs' scheduler all share it. The moment any event is ingested the
  writer singleton is live and open, and from then on *every* web read and *every* jobs run that
  calls `connect_read_only()` raises. That is a 500 on the dashboard on **every real run past the
  first ingested event**. It is currently masked only because jobs rewrote its tests to use
  short-lived `EventWriter` context managers that close before reading — which sidesteps it in
  tests and does **not** reflect how `bam serve` actually runs. This is exactly the class of bug a
  green test suite hides.
  **Fix dispatched:** make `connect_read_only()` writer-aware — hand back a `.cursor()` off the
  live connection when a writer for that path exists (verified working: duckdb cursors share one
  connection via MVCC and dodge the config check), falling back to a fresh read-only connect only
  when no writer is live. Public signature unchanged. Red-first test required: get_writer → write →
  flush → connect_read_only → query must FAIL today.
  **CONFIRMED INDEPENDENTLY BY 📜 jsonl TOO — and it is now breaking `just check`** via
  `tests/e2e/test_dashboard.py`. So the gate is RED until store lands this. Three panes hit the
  same wall from three directions; that is the system working.
  **PROCESS NOTE:** ⚙️ jobs and 📜 jsonl each self-committed (`61ede57`, `ad25e17`). Commits are
  lead-owned at phase boundaries; no harm done, both are folded into INTEGRATE, and both panes have
  been told to leave committing to the lead from here.

- **OQ-02 — CLOSED. Fixed at the source by 🧱 store (`b4a47a2`), independently verified by
  ✅ validator (red-first reproduced 5/5, idempotency proven under cross-thread collisions), and
  📡 otlp's local `_flush_lock` stopgap removed (`30cb2b0`) only AFTER proving its concurrency test
  still passes without it against the fixed writer — so the fix genuinely covers the otlp path
  rather than being masked by the stopgap. The concurrency tests stay as regression guards on the
  shared singleton. Original finding below.** 📡 otlp found a genuine race in
  `store/writer.py`: `EventWriter.flush()` holds the lock only around the buffer swap and releases
  it *before* `_flush_batch` runs `BEGIN TRANSACTION`/`DELETE`/`INSERT`/`COMMIT` on the one shared
  connection. Six concurrent posts through a single `get_writer()` singleton reliably raise
  `_duckdb.TransactionException: cannot start a transaction within a transaction`. This violates
  BL-01's own promise that `get_writer()` is safe to call from the OTLP handler, the JSONL scanner
  and the LangSmith poller at once. Today only otlp calls it (and has a local `_flush_lock`
  stopgap); the moment jsonl + langsmith go live this becomes an intermittent, data-losing
  heisenbug. Dispatched to 🧱 store to fix RED-FIRST (concurrent-flush test must fail first).
  **📡 otlp handled this exactly right: it mitigated locally, filed the OQ, and refused to edit
  another pane's file.** That is the discipline working.
- **OQ-03 — OPEN, accepted risk.** otlp's fixtures were built from the published
  `code.claude.com/docs/en/monitoring-usage` schema, not captured from live Claude Code traffic
  (capturing it from inside a build pane is self-referential). This is the spec's named RISK #1.
  The parser is defensive by design — unmapped attributes land in `payload`, unknown event names
  are stored as-is — so drift degrades rather than crashes. **GATE mitigation:** the validator's
  live-telemetry step (one real `claude -p` session against `bam serve`) doubles as the real-payload
  diff against these fixtures. If an attribute name differs, it is a one-line fix to
  `_ATTR_TO_COLUMN` in `ingest/otlp.py`, not a redesign.

- **OQ-01 — ANSWERED (human decision, 2026-07-11).** Raised by ⚙️ jobs: the repo's Stop hook ran
  `uv run pyrefly check` repo-wide with `exit 2`, force-continuing every IDLE pane over OTHER
  panes' work-in-progress type errors. That is a direct threat to exclusive file ownership — a
  pane that cannot stop starts editing files it does not own. **Resolution:** the human authorized
  the orchestrator to NEUTRALIZE the Stop hook for the run (only `.hooks.Stop` removed from
  `.claude/settings.json`; all other hooks untouched; verbatim original backed up).
  **The definition of done is UNCHANGED** — pyrefly is still fully enforced inside `just check` and
  `.github/workflows/ci.yml`, and GATE still requires a green `just check`. The 13 errors are REAL
  and remain owned by their panes (9 → 🧱 store, 4 → 🖥 web; dispatched). Full triage in
  `.open-questions.md`. Good catch by ⚙️ jobs — this was a hazard, not noise.

## Deferred

- **✅ CLOSED — `.claude/settings.json` Stop hook RESTORED** by the orchestrator to its exact
  pre-build state (verbatim; `git status` clean; the restored hook runs 0 errors / exit 0). OQ-01
  is closed, NOT deferred. Original note: Owner was **🤖 orchestrator** (NOT the lead,
  NOT any pane — nobody else edits that file). The Stop hook was removed for the duration of this
  run per OQ-01 and MUST be restored from the backup **before GATE**. ✅ validator: confirm the
  restore happened as part of the GATE checklist — a run that ends with the human's hook still
  missing is a failed run, no matter how green the tests are.
- **CI has never run on GitHub.** `.github/workflows/ci.yml` mirrors `just check` exactly and is
  validated BY CONSTRUCTION this run; no push happens. The human's first push is its first real run.
