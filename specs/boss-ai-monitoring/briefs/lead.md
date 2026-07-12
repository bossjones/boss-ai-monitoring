# lead.md — 👑 lead brief (Phase 1: Repo Scaffold + TDD Harness, in place)

> Derived from `../boss-ai-monitoring.html` (canonical) on 2026-07-11. Read `shared.md` first.
> If this conflicts with the HTML or observed behavior, the evidence wins — file an OQ.

**You own:** `pyproject.toml`, `uv.lock`, `justfile`, `.github/workflows/ci.yml`,
`.pre-commit-config.yaml`, `.env.sample`, `README.md`, `LICENSE`,
`src/boss_ai_monitoring/{__init__.py,config.py,cli.py}`, ALL package skeletons (the empty
`__init__.py` in `store/`, `ingest/`, `web/`, `jobs/` and across the `tests/` tree — scaffolded
once in Wave 0, then untouched), `tests/conftest.py`,
`tests/unit/{test_config.py,test_cli.py}`, `tests/test_smoke.py`, `config.sample.yaml`, the
board. You also dispatch all work, issue LOAN TICKETS, triage the shared append-only backlog and
open-questions files, and commit at phase boundaries.

## 1. Package skeleton, in place

- **No repo creation.** This repo exists and is already named `boss-ai-monitoring`. Never run
  `gh repo create`.
- `uv init --package --python 3.13 --name boss-ai-monitoring` at the repo root — hatchling build
  backend, `src/` layout, project metadata, MIT `LICENSE`. Confirm existing `specs/`, `prompts/`,
  `.gitignore`, the env files, `CLAUDE.md` are untouched. The pre-existing root `README.md` is a
  placeholder you EXTEND in place (its sections are marked *(filled in by the build)*).
- `uv add fastapi uvicorn duckdb jinja2 httpx langsmith pydantic pydantic-settings pyyaml
  sse-starlette`, `uv add --dev pytest pytest-asyncio respx ruff pyrefly codespell pre-commit
  playwright`, and `uv add --group notebooks marimo` (ALL dep adds are yours —
  pyproject.toml/uv.lock have one writer; ⚙️ jobs only writes the notebook file in Wave 4). Then
  **immediately** `uv run playwright install chromium` (the package ships no browser; Phase 6's
  smoke test needs it).
- **Scaffold all package skeletons NOW**: empty `__init__.py` for `store/`, `ingest/`, `web/`,
  `jobs/` and the `tests/` tree, plus a minimal `tests/conftest.py` (shared fixtures: tmp-DB
  path, TestClient factory). Workers never create these — that's how two panes writing into
  `ingest/` avoid racing on the same file.
- **`bam` console script**: `[project.scripts] bam = "boss_ai_monitoring.cli:main"`, RED-FIRST
  test in `tests/unit/test_cli.py`. Two subcommands:
  - `bam serve` — the spec's documented quickstart. Serving model (decided rev 4): ONE FastAPI
    app (web/app.py, otlp router mounted), TWO uvicorn Server instances in one asyncio loop —
    `server.dashboard_port` (8000) and `server.otlp_port` (4318), same app object.
  - `bam config db-path` — prints the RESOLVED DuckDB path from `BamSettings`. GATE's duckdb
    checks use `duckdb "$(uv run bam config db-path)"`; a bare `$BAM_DB_PATH` is unset in pane
    shells and silently opens an empty DB.
- `justfile`: `check` = ruff check + ruff format --check + pyrefly check + codespell + pytest;
  `dev` = `uv run bam serve` with reload (both ports must be live in dev — the OTLP curl and
  live-telemetry checks hit :4318); `fmt`; `docker-build`.
- `.github/workflows/ci.yml`: uv setup → `just check`. It must exactly mirror `just check` — it
  is validated BY CONSTRUCTION this run (no push happens; record its first real GitHub run as a
  DEFERRED board item).
- `.pre-commit-config.yaml`.
- `.env.sample` — extend the existing file, don't replace it. Document:
  `CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_METRICS_EXPORTER=otlp`, `OTEL_LOGS_EXPORTER=otlp`,
  `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`,
  `TRACE_TO_LANGSMITH`, `CC_LANGSMITH_API_KEY`, `CC_LANGSMITH_PROJECT`, `LANGSMITH_API_KEY`,
  `LANGSMITH_ENDPOINT`, `LANGSMITH_WORKSPACE_ID`, `LANGSMITH_PROJECT`, `BAM_DB_PATH`,
  `BAM_CLAUDE_PROJECTS_DIR`.

> RUN NOTE: the spec's Phase 1 says "Commit scaffold; push; confirm CI green on GitHub" — this
> run does NOT push (prompt G12). Commit locally; CI-on-GitHub is the deferred item.

## 2. Prove the TDD harness (RED → GREEN)

- Failing smoke test `tests/test_smoke.py::test_package_imports` asserting
  `boss_ai_monitoring.__version__` exists — watch it fail, then make it pass.

## 3. Configuration module (TDD)

- RED: tests for `config.py` — a pydantic-settings `BaseSettings` model (`BamSettings`) with
  **nested sections**:
  - `server`: dashboard port/bind, OTLP port/bind
  - `store`: db_path
  - `ingest`: jsonl scan interval, claude projects dir, langsmith poll interval + project
  - `jobs`: per-job enable flags
- Precedence: **env vars (prefix `BAM_`, nested via `__`) > YAML file (`config.yaml` or
  `$BAM_CONFIG` path, via `YamlConfigSettingsSource`) > code defaults.**
- GREEN: missing YAML file is fine (defaults apply); malformed YAML raises a clear startup error
  naming the file and line.
- Ship a commented `config.sample.yaml`; document precedence in README; `.env.sample` stays the
  env-var reference.
- Test approach: tmp YAML files + monkeypatched env, asserting precedence and error paths.

## 4. Unblock downstream immediately

Publish `store/writer.py`'s intended interface (function signatures + the event dict shape from
`shared.md`) to the backlog BEFORE store starts, so every ingest pane can write RED tests against
a stub without waiting on store's GREEN.

## Acceptance (paste output to the board before dispatching anyone)

- `just check` exits 0 — full lint/format/type/spell/test gate green.
- `uv run pytest tests/test_smoke.py tests/unit -q` green, red-first evidence recorded.

## Later (Wave 4 backlog item comes back to you)

Fold into `README.md`: quickstart (enable CC telemetry env vars → `uv run bam serve` → open
dashboard), architecture diagram, marimo usage (`uvx marimo edit notebooks/explore.py`, `marimo
run` app mode), LangSmith plugin setup, privacy notes (content-capture flags stay off), and a
back-reference link to `specs/boss-ai-monitoring/boss-ai-monitoring.html`.
