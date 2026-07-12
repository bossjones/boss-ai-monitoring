# Outstanding — the executable backlog

**This file is a plan.** Run it with:

```bash
/agent-harness:build specs/outstanding.md      # use opus
```

It is written to be executed, not admired: every item names the real files, the red-first test to
write, and the acceptance criterion. Written honestly — if something here reads like an excuse,
treat it as a bug report.

## Context

`boss-ai-monitoring` is merged to `main` (PR #1, squash). A single-user observability dashboard for
Claude Code: three ingest sources (OTLP `:4318`, `~/.claude/projects` JSONL transcripts, LangSmith
poller) → one DuckDB file → FastAPI + htmx on `:8000`. `just check` is green (ruff, ruff format,
pyrefly 0 errors, codespell, 236 tests) and CI is green. Screenshots and terminal evidence:
[`docs/PROOF.md`](../docs/PROOF.md).

**Read [`CLAUDE.md`](../CLAUDE.md) first** — conventions (uv only, pyrefly not mypy, TDD red-first,
never push without the human), the DuckDB cross-process lock and `bam snapshot`, and the multi-agent
gotchas all live there and are not repeated here.

Nothing below blocks the app from working.

---

## Backlog

### P1 — LangSmith polling is silently disabled by a config-name mismatch

**Defect.** The poller starts only when `ingest.langsmith_project` is set, i.e. env
`BAM_INGEST__LANGSMITH_PROJECT` (keys are `BAM_` + nested section, G4). The *ambient*
`LANGSMITH_PROJECT` that direnv exports for the `langsmith` CLI is **not** read by the app —
`config.py` is the only thing allowed to touch the environment and it does not alias that name.
With no project set the poller logs once and stays down, so LangSmith rows never appear and the
feature looks broken.

**This is a decision, not just code — do not silently pick one:**

- **(a)** alias the ambient `LANGSMITH_PROJECT` / `CC_LANGSMITH_PROJECT` in `config.py` (convenient;
  slightly weakens "config is the only env reader"), or
- **(b)** keep the strict key and make the omission **loud** at startup — a visible warning, and a
  badge on the dashboard, rather than a single log line nobody reads.

Ask the human which. If forced to choose, prefer **(b)**: it preserves G4 and makes a silent
failure loud, which is the whole point of this tool.

- **Files:** `src/boss_ai_monitoring/config.py`, `src/boss_ai_monitoring/cli.py`,
  `tests/unit/test_config.py`
- **Red-first test:** with no project configured, the app surfaces the omission (warning/badge);
  with `BAM_INGEST__LANGSMITH_PROJECT` set, the poller starts.
- **Acceptance:** a fresh user with only the ambient env vars either gets LangSmith ingest or is
  *told, visibly*, why they don't.

### P1 — `cost_per_successful_task` divides two different populations

**Defect.** `v_five_metrics.cost_per_successful_task` divides **OTel-derived cost** by a task count
drawn from **every event in the database**. Cost exists only for OTel events (~200); the task count
spans the full 107k-row JSONL backfill, nearly all of which carries no cost. The result collapses
toward zero — it read `$0.00042` on the real dataset.

Already mitigated, **not fixed**: sub-cent values render as `<$0.01` instead of a false `$0.00`
(`web/templates/partials/costs_fragment.html`), because a monitoring tool reporting a non-zero cost
as zero is lying. The metric itself still needs its denominator scoped to its numerator.

- **Files:** `src/boss_ai_monitoring/store/views.sql`, `tests/unit/store/test_views.py`
- **Red-first test:** golden fixture — N tasks with OTel cost + M JSONL-only tasks; the metric must
  equal `total_cost / N`, not `total_cost / (N+M)`.
- **Fix:** count only tasks that *have* an OTel-derived cost (or window numerator and denominator to
  the same period). Pick one and say why in the SQL comment.
- **Acceptance:** on the real DB the value is a plausible per-task cost (cents, not ten-thousandths).

### P2 — `autonomy_score` and `recovery_rate` have never run on real data

**Defect.** Both are always `NULL` (render `—`) because no dataset yet contains `tool_decision` or
`api_error` events. The degradation is *correct* — show nothing rather than invent a number — but it
means neither metric has ever been exercised outside synthetic fixtures.

- **Files:** `tests/fixtures/otlp/`, `src/boss_ai_monitoring/store/views.sql`
- **How:** capture a real session that *does* emit them (a tool denial → `tool_decision`; a forced
  API failure → `api_error`) using the wire-level capture recipe that produced
  `tests/fixtures/otlp/real_session_logs.json`. **Sanitize PII** (`user.email`, `user.id`,
  `user.account_uuid`, `organization.id`, `session.id`) exactly as that fixture did.
- **Acceptance:** both metrics compute a non-`NULL` value from a real captured payload.

### P2 — JSONL backfill is unbounded

**Defect (maybe).** The transcript reader ingested **107,590 events** — the entire
`~/.claude/projects` history. There is no `--since` window and no retention policy. That is fine for
a laptop tool and it is *why* the historical cost chart has anything in it. But the first run does a
lot of work, and the JSONL row count dwarfing OTel's is what skews the metric above.

- **Files:** `src/boss_ai_monitoring/ingest/jsonl.py`, `src/boss_ai_monitoring/config.py`
- **Ask the human first** whether a bounded window is even wanted. Do not implement a retention
  policy that quietly deletes their history.

### P3 — Six real OTLP event types are stored but never surfaced

A real session emits `hook_registered`, `plugin_loaded`, `mcp_server_connection`,
`hook_execution_start`, `hook_execution_complete`, `assistant_response`. They land raw in the
`payload` JSON column and nothing is dropped (proven by
`tests/unit/ingest/test_otlp.py::TestRealCapturedSession`), but none is promoted to a first-class
column or any view. If hook/plugin/MCP telemetry is interesting, that work is unstarted.

- **Files:** `src/boss_ai_monitoring/store/schema.py`, `src/boss_ai_monitoring/store/views.sql`

---

## Non-goals — do NOT "fix" these

A build agent will try. Don't.

- **No auth; localhost bind.** Single-user, single-host, trusted-network (G10). Multi-user
  aggregation is explicitly out of scope.
- **Heuristic scoring only, no LLM judge** (G9) — Anthropic's own finding was that LLM-generated
  definitions "encoded the very ambiguities we were trying to eliminate."
- **OTel content-capture flags stay OFF** (G8). Verified at the wire: Claude Code itself sends
  `prompt=<REDACTED>` when they're off. A test fails loudly if that ever changes.
- **DuckDB's cross-process lock.** Inherent to the single-file/single-writer design. `bam snapshot`
  is the supported answer; it is not a workaround for a bug.

---

## Verification — tests alone are not enough

Both of the worst bugs in this project's history passed a fully green test suite. Do all three:

1. `just check` — ruff + ruff format + pyrefly (0 errors) + codespell + full pytest.
2. **Live run on a FRESH, EMPTY database** — this is the step that catches what tests can't:
   ```bash
   BAM_STORE__DB_PATH=/tmp/fresh.duckdb uv run bam serve
   ```
   Rows must arrive **unattended** (nothing posted, nothing run by hand) *and* the dashboard must
   stay responsive (`curl -m 3 localhost:8000/` in ~10ms) **while** the backfill runs.
3. **Read the data back out while the app is still up:**
   ```bash
   duckdb "$(uv run bam snapshot)" "SELECT source, count(*) FROM events GROUP BY 1"
   ```

Then re-shoot the evidence if the UI changed: `uv run python scripts/capture_screenshots.py`.

---

## Resolved (kept — this is the cautionary tale)

| Item | Resolution |
|---|---|
| OQ-01 — Stop hook blocked idle panes on *other* panes' errors | Session-scoped hook (`.claude/hooks/pyrefly_session_scope.py`) |
| OQ-02 — `flush()` raced the shared connection | Lock held across the whole DB round-trip (`30cb2b0`) |
| OQ-03 — OTLP fixtures never captured from a live session | Captured at the wire, sanitized, committed |
| OQ-04 — `connect_read_only()` vs a live in-process writer | Writer-aware MVCC cursor (`1c0c938`) |
| OQ-05 — DuckDB lock is exclusive cross-process | `bam snapshot` |
| `snapshot_dir` didn't follow the DB (Docker snapshots lost) | Derived from `db_path.parent` in `config.py` |
| **`bam serve` never started the JSONL scanner, LangSmith poller, or job scheduler** | Supervised background tasks in `cli.py::_serve_both` |
| **`scan_once` blocked the event loop**, freezing the dashboard for the whole backfill | `asyncio.to_thread`; same for sync job callables |
| **Job results were never persisted** → `drift: unknown` forever | `persist=` wired in `cli.py::_build_job_scheduler` |
| `cost_per_successful_task` rendered a false `$0.00` | Sub-cent renders `<$0.01` |
| CI had never run on GitHub | Ran on the PR push; green |

### The one that nearly shipped

Two documentation agents, reading the code independently, both reported that `bam serve` started
only uvicorn — the JSONL scanner, the LangSmith poller and the trailing jobs were implemented,
tested, and **never called in production**. It was true. The app looked healthy because OTLP is an
HTTP route and works regardless, and because the JSONL/LangSmith rows in the database had been
ingested *by hand* during the build's gate. The visible symptom nobody chased was `drift: unknown`
in the provenance footer — the drift job had never run once.

Wiring the loops in immediately exposed a second bug: `scan_once` is synchronous and takes minutes
over a real `~/.claude/projects`, so awaiting it on the event loop froze the dashboard, the OTLP
receiver and SSE for the entire backfill. Fixing *that* exposed a third: the scheduler was built
without `persist=`, so the jobs ran and their results went nowhere — `drift: unknown` was still on
screen, now for a different reason.

**The lesson, in one line: a green test suite told us nothing here.** The tests called the loops
directly, so they passed while nothing in production called them at all. What found it was asking an
agent to *explain the system in prose*, and what confirmed the fixes was *running the real app on an
empty database*.

---

## Report

State plainly, with evidence pasted, not described:

1. Which backlog items were completed, and which were deliberately left (and why).
2. `just check` output — must be green, zero pyrefly errors.
3. The fresh-DB live run: rows arrived unattended (paste the `duckdb "$(uv run bam snapshot)"`
   counts) and the dashboard stayed responsive during backfill (paste the curl timings).
4. Any decision that needed the human (P1 LangSmith; P2 retention) — say what you asked and what
   they chose.
5. Anything you found that contradicted this file. The evidence wins over the plan; report the
   contradiction rather than softening it.
