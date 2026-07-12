# Outstanding

What is **not** done, as of 2026-07-12 (post-build, post-`bam snapshot`). Written to be honest
rather than flattering: if something here reads like an excuse, treat it as a bug report.

Nothing in this list blocks the app from working. `just check` is green (ruff, ruff format,
pyrefly 0 errors, codespell, full pytest), CI is green, and all three ingest sources are live —
see [`docs/PROOF.md`](../docs/PROOF.md).

---

## 0. LangSmith polling needs `BAM_INGEST__LANGSMITH_PROJECT`, not `LANGSMITH_PROJECT`

**Status:** works as designed, but it is an onboarding trap.

The poller starts only when `ingest.langsmith_project` is set — i.e. the env var
`BAM_INGEST__LANGSMITH_PROJECT` (config keys are `BAM_` + nested section, G4). The *ambient*
`LANGSMITH_PROJECT` that direnv exports for the `langsmith` CLI is **not** read by the app; config
is the only thing allowed to read the environment, and it does not alias that name.

With no project configured the poller logs and stays down (graceful degradation, correct) — so
LangSmith rows silently never appear and it looks like the feature is broken. Either alias the
ambient name in `config.py` or make the omission louder.

## 1. `cost_per_successful_task` mixes two populations

**Status:** real defect, partially mitigated.

`v_five_metrics.cost_per_successful_task` divides **OTel-derived cost** by a task count taken from
**every event in the database**. But cost only exists for OTel events (~800), while the task count
spans the full 107k-row JSONL backfill, most of which carries no cost. The result is diluted toward
zero: it read `$0.00042` on the real dataset.

Mitigated, not fixed: sub-cent values now render as `<$0.01` instead of a false `$0.00`
(`web/templates/partials/costs_fragment.html`), because a monitoring tool reporting a non-zero cost
as zero is a lie. The metric itself still needs its denominator scoped to the same population as
its numerator — either count only tasks that *have* an OTel cost, or window both to the same
period.

## 2. `autonomy_score` and `recovery_rate` are always `NULL`

**Status:** unproven, not broken.

Both render as `—` on the live dashboard because the dataset contains no `tool_decision` or
`api_error` events. The degradation is correct (show nothing rather than invent a number), but it
means **neither metric has ever been exercised against real data.** They are tested only against
synthetic fixtures. Until a session produces those event types, treat both as unverified.

## 3. JSONL backfill is unbounded

**Status:** works as built; may not be what you want.

The transcript reader ingested **107,590 events** — the entire `~/.claude/projects` history. There
is no bounded-window or retention option. That is fine for a single-user tool on a laptop, and it
is why the historical cost chart has anything in it at all. But it also means:

- the first run does a large amount of work,
- the JSONL row count dwarfs OTel's, which is what skews item 1 above.

If a `--since` window or a retention policy is wanted, it does not exist yet.

## 4. LangSmith `thread_id` ↔ `session.id` join is best-effort

**Status:** known, by design (RISK #2 from the spec).

Runs that cannot be matched to a Claude Code session land in a **visible LangSmith-only bucket**;
they are never silently merged into a `session.id`. This is the safe failure mode, but the join
remains heuristic. 50/50 runs matched in the live read-back — a sample of one project on one day.

## 5. OTLP event coverage is broader than the schema models

**Status:** verified safe, worth revisiting (closes OQ-03).

Capturing a **real** telemetry session (Claude Code 2.1.207, wire-level, 2026-07-11) found six
event types that no doc-derived fixture covered: `hook_registered`, `plugin_loaded`,
`mcp_server_connection`, `hook_execution_start`, `hook_execution_complete`, `assistant_response`.

They are stored raw in the `payload` JSON column and nothing is dropped — proven by
`tests/unit/ingest/test_otlp.py::TestRealCapturedSession` against the sanitized real capture
(`tests/fixtures/otlp/real_session_logs.json`). But none of them are *promoted* to first-class
columns or surfaced in any view. If hook/plugin/MCP telemetry is interesting, that work is unstarted.

Also confirmed at the wire: with the content-capture flags off (the default), Claude Code sends
`prompt=<REDACTED>` itself — only `prompt_length` survives. G8 holds, and there is now a test that
fails loudly if a future version stops redacting.

## 6. Deliberate non-goals — not gaps, do not "fix"

- **No auth; dashboard and OTLP bind localhost.** Single-user, single-host, trusted-network scope.
  Multi-user aggregation is explicitly out of scope (G10).
- **v1 quality scoring is deterministic/heuristic — no LLM judge** (G9). Anthropic's own warning:
  LLM-generated definitions "encoded the very ambiguities we were trying to eliminate."
- **OTel content-capture flags stay OFF by default** (G8). Telemetry is metadata unless you opt in.
- **DuckDB's cross-process lock.** Inherent to the single-file/single-writer design. `bam snapshot`
  is the supported way around it; it is not a workaround for a bug.

---

## Resolved during this work (kept for the record)

| Item | Resolution |
|---|---|
| OQ-01 — Stop hook blocked idle panes on other panes' errors | Session-scoped hook (`.claude/hooks/pyrefly_session_scope.py`) |
| OQ-02 — `flush()` raced the shared connection | Lock held across the whole DB round-trip (`30cb2b0`) |
| OQ-03 — OTLP fixtures never captured from a live session | Captured, sanitized, committed; see item 5 |
| OQ-04 — `connect_read_only()` vs a live in-process writer | Writer-aware MVCC cursor (`1c0c938`) |
| OQ-05 — DuckDB lock is exclusive cross-process | `bam snapshot` |
| `snapshot_dir` did not follow the DB (broke Docker volumes) | Derived from `db_path.parent` in `config.py` |
| **`bam serve` never started the JSONL scanner, LangSmith poller, or job scheduler** | Wired as supervised background tasks in `cli.py::_serve_both` |
| **`scan_once` blocked the event loop**, freezing the dashboard during backfill | Runs via `asyncio.to_thread`; same for sync job callables |
| CI had never run on GitHub | Ran on the PR push; green |

### The one that nearly shipped

Two documentation agents, reading the code independently, both reported that `bam serve` started
only uvicorn — the JSONL scanner, the LangSmith poller and the trailing jobs were implemented,
tested, and **never called in production**. It was true. The app looked healthy because OTLP is an
HTTP route and works regardless, and the JSONL/LangSmith rows in the database had been ingested *by
hand* during the build's gate. The visible symptom nobody chased was `drift: unknown` in the
provenance footer — the drift job had never run once.

Wiring the loops in then exposed a second bug immediately: `scan_once` is synchronous and takes
minutes over a real `~/.claude/projects`, so awaiting it on the event loop froze the dashboard, the
OTLP receiver and SSE for the whole backfill. Both are fixed and covered by tests
(`tests/unit/test_cli.py::TestServeStartsBackgroundWork`,
`tests/unit/ingest/test_jsonl.py::test_scan_does_not_block_the_event_loop`).
