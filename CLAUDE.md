# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`boss-ai-monitoring` is a single-user observability dashboard for Claude Code usage — cost, tokens,
tool stats, and session timelines — fed by three ingest sources (OTLP telemetry, `~/.claude/projects`
JSONL transcripts, LangSmith runs) into one DuckDB file, served by a FastAPI + Jinja2 + htmx app.

The repo is currently **pre-build**: the Python package does not exist yet. Everything is specified in
two authoritative documents:

- `specs/boss-ai-monitoring/boss-ai-monitoring.html` — the full 9-phase spec (read-only reference;
  do not edit its inline status markers during a build run).
- `prompts/boss-ai-monitoring-build-team.md` — the multi-agent cmux build prompt that implements the
  spec. Its BINDING LESSONS, GROUND TRUTHS (G1–G14), and file-ownership map govern any build work
  here, even outside a team run.

## Commands

Once the package is scaffolded (`uv init --package --python 3.13`), the workflow is:

```bash
just check                 # definition of done: ruff check + ruff format --check + pyrefly + codespell + pytest
uv run pytest -q           # full test suite
uv run pytest tests/unit/store -q         # one suite
uv run pytest tests/unit/store/test_writer.py::test_name -q   # one test
uvx ruff check <file>      # lint one file
uv run pyrefly check       # type check (pyrefly — NOT mypy/ty/basedpyright)
just dev                   # run the app locally (uvicorn reload); later: uv run bam serve
docker compose up --build  # containerized app: ports 8000 (dashboard) + 4318 (OTLP)
```

Verification CLIs (installed on this machine):

```bash
duckdb "$BAM_DB_PATH" "SELECT source, count(*) FROM events GROUP BY 1"   # row counts per ingest source
langsmith run list --project "$LANGSMITH_PROJECT"                        # LangSmith read-back (auth is ambient via direnv)
```

## Non-negotiable conventions

- **Python via `uv` only** (`uv run`, `uvx`); Python 3.13; src/ layout (`src/boss_ai_monitoring/`).
- **Type checker is pyrefly.** Use the `/agent-harness:pyrefly-typing` skill for annotation help.
- **TDD red-first**: the failing test exists and fails before the implementation does.
- **Git: commit locally on the current branch; NEVER push.** The human pushes and reviews. No
  `gh repo create` — the `bossjones/boss-ai-monitoring` remote already exists.
- **Config discipline**: all settings flow through `config.py` (`BamSettings`, pydantic-settings);
  precedence env (`BAM_` prefix, `__` nesting) > YAML > defaults. No ad-hoc `os.environ` reads.
- **DuckDB single-writer**: exactly one write connection, owned by `store/writer.py`. Notebooks and
  everything else open read-only connections.
- **OTLP is http/json only** on :4318 — no gRPC, no otel-collector.
- **Frontend**: htmx vendored as a single static file; no npm, no build step.
- **Privacy**: OTel content-capture flags stay OFF by default; `~/.claude/projects` is mounted
  read-only wherever it is read.

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
- Planned by the build: `src/boss_ai_monitoring/{config.py,store/,ingest/,web/,jobs/}`,
  `tests/{unit,integration,e2e}/`, `justfile`, `compose.yaml`, `notebooks/explore.py`
