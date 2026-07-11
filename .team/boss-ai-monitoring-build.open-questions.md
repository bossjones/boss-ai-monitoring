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
(end of current questions)
