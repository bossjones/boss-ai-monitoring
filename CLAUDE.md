# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`boss-ai-monitoring` is a single-user observability dashboard for Claude Code usage — cost, tokens,
tool stats, and session timelines — fed by three ingest sources (OTLP telemetry, `~/.claude/projects`
JSONL transcripts, LangSmith runs) into one DuckDB file, served by a FastAPI + Jinja2 + htmx app.

The package is **built** (Phases 1–9 landed 2026-07-11 by a 7-pane cmux team; `just check` green,
208 tests). Two authoritative documents still govern any further work:

- `specs/boss-ai-monitoring/boss-ai-monitoring.html` — the full 9-phase spec, canonical and
  human-facing (read-only reference; do not edit its inline status markers during a build run).
  Agent-facing working copies live in `specs/boss-ai-monitoring/briefs/` — one scoped markdown
  brief per build-team pane plus `shared.md`; read those instead of the HTML for day-to-day
  reference. If a brief and the HTML disagree, the HTML wins.
- `prompts/boss-ai-monitoring-build-team.md` — the multi-agent cmux build prompt that implements the
  spec. Its BINDING LESSONS, GROUND TRUTHS (G1–G14), and file-ownership map govern any build work
  here, even outside a team run.

## Commands

```bash
just check                 # definition of done: ruff check + ruff format --check + pyrefly + codespell + pytest
uv run pytest -q           # full test suite
uv run pytest tests/unit/store -q         # one suite
uv run pytest tests/unit/store/test_writer.py::test_name -q   # one test
uvx ruff check <file>      # lint one file
uv run pyrefly check       # type check (pyrefly — NOT mypy/ty/basedpyright)
just dev                   # run the app locally with reload (wraps uv run bam serve — both ports)
docker compose up --build  # containerized app: ports 8000 (dashboard) + 4318 (OTLP)
uv run bam config db-path  # print the resolved DuckDB path (use this in shell commands, never a bare $BAM_DB_PATH)
```

Verification CLIs (installed on this machine):

```bash
# ALWAYS the resolved-path form — a bare $BAM_DB_PATH is unset in most shells and silently
# opens an EMPTY db, which reads as a fake-green result.
duckdb "$(uv run bam config db-path)" "SELECT source, count(*) FROM events GROUP BY 1"

# ...but if `bam serve` is RUNNING and has written anything, that errors on the file lock.
# Snapshot instead — works while it serves, and composes because it prints only the path:
duckdb "$(uv run bam snapshot)" "SELECT source, count(*) FROM events GROUP BY 1"
BAM_STORE__DB_PATH="$(uv run bam snapshot)" uvx marimo edit notebooks/explore.py

# `bam snapshot` POSTs to the DASHBOARD port. If `serve` runs on a non-default port, pass the SAME
# BAM_SERVER__* env or it misses the live app, falls back in-process, and prints an EMPTY path.
langsmith run list --project "$LANGSMITH_PROJECT"   # LangSmith read-back (auth is ambient via direnv)
rtk proxy <cmd>   # rtk FILTERS output — prefix any command whose full output is evidence
```

## Non-negotiable conventions

- **Python via `uv` only** (`uv run`, `uvx`); Python 3.13; src/ layout (`src/boss_ai_monitoring/`).
- **Type checker is pyrefly.** Use the `/agent-harness:pyrefly-typing` skill for annotation help.
- **TDD red-first**: the failing test exists and fails before the implementation does.
- **A green `just check` is NOT evidence the app works.** It was green (256 tests) through a
  crash-looping jsonl scanner, a job that had never once fired, and two OTLP 500s — the suite
  tests components in isolation and never the concurrent whole. Definition of done for anything
  touching ingest: run the REAL app on an EMPTY db and watch data arrive unattended
  (`BAM_STORE__DB_PATH=/tmp/fresh.duckdb uv run bam serve`) across several scan intervals AND a
  LangSmith poll — then confirm rows LANDED. No errors alone can just mean a silent no-op.
- **Fixtures must match the shapes real ingest produces.** `correction_scan` read
  `payload["text"]` for months; 0 of 4522 real prompts have that key (prompt text lives at
  `payload.message.content` — a plain string OR a content-block array). Its tests passed happily
  against a payload shape no source has ever written. Check a new fixture against `duckdb` first.
- **Git: commit locally on the current branch; do NOT push unless the human explicitly asks.**
  During an autonomous build run, never push — the human reviews first. No `gh repo create`; the
  `bossjones/boss-ai-monitoring` remote already exists.
- **Config discipline**: all settings flow through `config.py` (`BamSettings`, pydantic-settings);
  precedence env (`BAM_` prefix, `__` nesting) > YAML > defaults. No ad-hoc `os.environ` reads.
- **DuckDB single-writer**: exactly one write connection, owned by `store/writer.py`. Readers use
  `connect_read_only()`, which is writer-aware *in-process*. **Cross-process the file lock is
  exclusive**: once the app has actually written (the writer is created LAZILY — an idle `bam
  serve` holds no lock), an outside `duckdb`/marimo process gets `IO Error: Could not set lock`.
  The spec's "readers never fight the writer" claim is only half true (OQ-04/OQ-05) — don't
  re-derive it. **Use `bam snapshot`** instead of stopping the app: it asks the running app for a
  consistent point-in-time copy (tables AND views, via `COPY FROM DATABASE` on the writer's own
  connection) and prints just the path, so it composes.
- **The whole `flush()` DB round-trip is lock-protected**, not just the buffer swap — releasing the
  lock before `BEGIN TRANSACTION` let concurrent producers race the shared connection (OQ-02).
- **DuckDB parks the pending result ON the connection**, so `execute()` + `fetchone()` is NOT
  atomic. Two threads sharing one connection means one silently gets the OTHER's rows. This is the
  generalization of OQ-02 that OQ-02 failed to state — and it bit again (OQ-06): unlocked
  `get_cursor()` on the jsonl worker thread fetched `langsmith_poll`'s
  `SELECT DISTINCT session_id FROM events`, so `int(cursor)` got a session UUID and crash-looped
  the scanner. The crash was the LUCKY case: the same race returns a valid-but-foreign byte offset
  or `None`, silently rewinding a transcript's cursor with no error at all.
  **Every `_conn` touch takes `self._lock`.** The connection is name-mangled (`__conn`); ruff `SLF`
  plus a test in `tests/unit/store/test_writer_concurrency.py` ban `._conn` outside `writer.py`.
  **Cross-module READS go through `writer.cursor()`** — an independent result set (MVCC, committed
  rows only) that can't be poisoned and never stalls a flush. Do NOT put reads on the write lock:
  `_known_session_ids` is a full table scan and would serialize against every ingest flush.
- **OTLP is http/json only** on :4318 — no gRPC, no otel-collector.
- **Frontend**: htmx vendored as a single static file; no npm, no build step.
- **Fast dev loop**: iterate against `uv run bam serve` locally; bring `docker compose` up the
  minimum number of times (packaging validation only). Never rebuild the image to test a code
  change — if compose gains external services, leave them running and point the local app at
  them via `BAM_*` env config.
- **Privacy**: OTel content-capture flags stay OFF by default; `~/.claude/projects` is mounted
  read-only wherever it is read.

## Multi-agent (cmux) runs — hard-won, do not re-learn

Observed live on 2026-07-11 during the 7-pane build. Full detail in
`.team/boss-ai-monitoring-build.board.md` ("LESSONS FOR THE NEXT RUN").

- **The lead pane goes idle after every turn.** Workers never self-dispatch and cannot see each
  other, so when the lead stops, the whole team stops *silently* — no error, no red pill. Run a
  background watchdog that re-drives the lead whenever it is idle and the board is not DONE. This
  was the single biggest time sink of the run (~19 automated nudges + 3 by hand).
- **Liveness = screen-diff, never a text marker.** md5 `cmux read-screen` twice ~10s apart; changed
  = working. Grepping for `esc to interrupt` gave a FALSE "idle" on a pane that was actively
  working — a false stall diagnosis is worse than a missed one.
- **`cmux new-workspace` / `new-split` ignore `--json`** (they print `OK workspace:9`). Resolve new
  surface UUIDs by diffing `cmux tree --all`. Short refs (`surface:N`) renumber; UUIDs are the only
  stable handle — persist them to a roster file.
- **Quote model args**: `--model opus[1m]` is glob-eaten by zsh (`no matches found`); use
  `--model 'opus[1m]'`. Same trap with any unquoted glob (`docker images -q foo*`).
- **The Claude composer renders ghost hint text** that drifts on its own and looks EXACTLY like a
  stranded missed-enter send. Probe with a real prompt before "fixing" it.
- **The `Stop` hook is session-scoped** (`.claude/hooks/pyrefly_session_scope.py`): it type-checks
  only the `.py` files *this* session edited, read from its own `transcript_path`. It used to run
  repo-wide with `exit 2`, which force-continued *idle* panes over *other* panes' WIP errors and
  pressured them into editing files they don't own (OQ-01). `git diff` scoping would NOT have
  fixed that — panes share one working tree, so the scope must be per-SESSION. The hook fails
  open; `just check` / pre-commit / CI still enforce full-tree pyrefly.
- **The validator must WRITE its log**, not just speak a verdict — a verdict that exists only on a
  scrollable terminal is not evidence. Confirm by artifact (git deltas, file mtimes, exit codes,
  duckdb counts), never by silence or by a Claude Code notification.

## Secrets and hooks

- A `pre_tool_use` hook blocks Bash commands containing the substrings `rm `, `--rm`, and the
  env-file token. Use `mv` to the scratchpad instead of `rm`, and never type paths containing that
  token (e.g. run bare `direnv allow` with no path argument).
- LangSmith auth is **ambient**: direnv exports `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`,
  `CC_LANGSMITH_*`, etc. into every shell. Never `cat` the env files or echo those values —
  presence checks only (`test -n "$LANGSMITH_PROJECT"`).
- direnv re-blocks its file after any edit to it; the symptom is a suddenly-unauthenticated
  `langsmith` CLI. Fix with bare `direnv allow`.

## Layout

- `specs/` — spec HTML + supporting images (read-only reference during builds)
- `prompts/` — build-team prompts and their lineage notes (`prompts/README.md`)
- `.claude/` — project Claude Code settings, hooks, status line
- `logs/` — hook event logs (chat.json, pre/post tool use, etc.)
- `.team/` — build-team durable state: board (FSM/roster/lessons), backlog, open questions,
  validator log. The board is the resume point for any confused or restarted agent.
- `src/boss_ai_monitoring/{config.py,cli.py,store/,ingest/,web/,jobs/}`,
  `tests/{unit,integration,e2e}/`, `justfile`, `compose.yaml`, `notebooks/explore.py`
