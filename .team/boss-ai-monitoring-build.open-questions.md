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
Status: NEEDS-HUMAN
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

---
(end of current questions)
