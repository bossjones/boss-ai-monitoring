# boss-ai-monitoring-build — BOARD (durable state)

> Any restarted or confused agent: **read this file and resume from the recorded state.**
> Lead-owned. Workers append to `.backlog.md` / `.open-questions.md` instead of editing this.

## FSM

```
SCAFFOLD ✅ -> STORE-CORE ▶ -> INGEST-FANOUT -> INTEGRATE -> PACKAGE -> GATE -> DONE
```

**Current state: STORE-CORE** (Wave 1 dispatched)

## Roster

| pane | role | phase(s) | state |
|---|---|---|---|
| 👑 lead | scaffold, config, cli, gate files, board | 1 | 🟢 SCAFFOLD done |
| 🧱 store | schema.py, writer.py, views.sql | 2 | 🔵 Wave 1 (critical path) |
| 📡 otlp | ingest/otlp.py | 3 | ⏸ waits on store GREEN |
| 📜 jsonl | ingest/jsonl.py, ingest/langsmith_poll.py | 4+5 | ⏸ waits on store GREEN |
| 🖥 web | web/**, e2e, docs/AGENT_LOOP.md | 6+7 | 🔵 Wave 1 SHADOW (hermetic scaffold) |
| ⚙️ jobs | jobs/**, notebook, Dockerfile, compose | 8+9 | 🔵 Wave 1 SHADOW (hermetic scaffold) |
| ✅ validator | validator-log.md, GATE checklist | GATE | ⏸ standing substitute |

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
| SCAFFOLD baseline | `feat(scaffold): uv package, config, bam CLI, TDD harness` | ✅ committed |
| STORE-CORE green | — | pending |
| INGEST-FANOUT green | — | pending |
| INTEGRATE green | — | pending |
| PACKAGE green | — | pending |
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

## Deferred

- **CI has never run on GitHub.** `.github/workflows/ci.yml` mirrors `just check` exactly and is
  validated BY CONSTRUCTION this run; no push happens. The human's first push is its first real run.
