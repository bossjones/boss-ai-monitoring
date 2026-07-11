# boss-cmux — boss-ai-monitoring BUILD run

> **STATUS: CURRENT** (rev 3, 2026-07-11). First prompt in this repo's lineage — there is no prior
> research/verify run to supersede. The spec ([`specs/boss-ai-monitoring/boss-ai-monitoring.html`](../specs/boss-ai-monitoring/boss-ai-monitoring.html))
> is already detailed, decided (every open question resolved via `AskUserQuestion` at authoring
> time), and phase-ordered — so this run goes straight to **build**, TDD red-first, implementing
> all 9 phases in place, in this repo.
>
> **Rev 2 changes** (post gap-analysis re-review): PREFLIGHT toolchain block (duckdb CLI +
> langsmith CLI now installed on this machine; docker-daemon + direnv-env checks); `bam serve`
> console script assigned to the lead; Phase 4 dedupe key corrected to `session_id + request_id`;
> `git_sha`/`cwd` transcript extraction, OTLP gzip/oversized-body edge cases, and the named
> fixture sets carried in from the spec; `uv run playwright install chromium` added to Wave 0;
> G13 (test layout authoritative) + G14 (ambient LangSmith auth + `langsmith` CLI read-back loop)
> added; DONE report gains a live-telemetry generation recipe and a LangSmith read-back
> cross-check (d2).
>
> **Rev 3 changes** (same day): per-pane spec briefs extracted to
> [`specs/boss-ai-monitoring/briefs/`](../specs/boss-ai-monitoring/briefs/) — the HTML stays the
> canonical human-facing spec; agents read `shared.md` + their role brief instead (schemas, edge
> cases, and acceptance criteria carried verbatim; deliberate run deviations flagged as
> `RUN NOTE:`). Kickoff messages and BINDING LESSON 7 updated accordingly.

## Why this run is shaped the way it is

This prompt borrows its bones from the `macos-ci` build-team lineage
(`/Users/bossjones/dev/bossjones/macos-ci/prompts/macos-ci-build-team.md`) — the same binding
lessons, exclusive-file-ownership discipline, TDD-red-first gate, FSM, status board, and
orchestrator heartbeat. Two things differ from that lineage, and both are load-bearing:

1. **This build lands IN this repo, in place — not a new standalone repo.** The spec HTML was
   originally drafted assuming a `gh repo create bossjones/boss-ai-monitoring --clone` breakout
   (mirroring `breakout-openobserve.md`, a pattern that lives in a *different* repo,
   `boss-skills`). That language has been corrected in the spec itself (2026-07-11): the repo
   this spec lives in is **already** named `boss-ai-monitoring` and is **already** a git repo.
   There is no repo to create. The team scaffolds a uv package at this repo's root, alongside
   the existing `specs/`, `prompts/`, `.env`, `.gitignore`.
2. **No hour-scale external build exists here** (no Packer/VM image to babysit), so there is no
   dedicated 📡 log-watcher pane or plain-shell build pane. All 7 panes are real Claude Code
   agents. The longest single operation is `docker compose up --build` (a few minutes for base
   image layers) — the validator watches that the same way it watches everything else: by
   artifacts and exit codes, never by pixels.

## The request that generated this prompt

> I want you to write me a prompt to invoke a multi agent team to implement
> `specs/boss-ai-monitoring/boss-ai-monitoring.html` using the `/boss-cmux` skill, in the same
> format as the macos-ci prompts, capturing the gotchas and edge cases learned there. The build
> must happen in this current repo (`/Users/bossjones/dev/bossjones/boss-ai-monitoring`), not a
> separately created GitHub repo. Lead pane = opus (smarter coordination); every worker pane =
> sonnet.

Decisions recorded at authoring time (2026-07-11): 7 panes, lead on **opus[1m]**, all 6 workers
on **sonnet**; no `gh repo create` — the repo already exists (the `bossjones/boss-ai-monitoring`
remote was published before this run), so the build team commits locally on the current branch and
does **not** push (the human pushes/reviews after the run); the spec HTML is treated as
**read-only reference** for this run — nobody edits its inline status markers, durable progress
lives in `.team/` instead.

---

## The prompt

Paste everything inside the fence into a fresh Claude Code session started in
`/Users/bossjones/dev/bossjones/boss-ai-monitoring`. That session is the ORCHESTRATOR.

````text
/boss-cmux Boot a 7-pane BUILD team for the repo /Users/bossjones/dev/bossjones/boss-ai-monitoring
and implement specs/boss-ai-monitoring/boss-ai-monitoring.html Phases 1-9, TDD red-first, entirely
in place in this repo.

You are the ORCHESTRATOR. You spawn the workspace, hand the lead its brief, then run the HEARTBEAT
loop (below) until DONE — you never go quiet on the human. Drive the LEAD only; the lead drives
the workers.

Reuse the open cmux window; add a NEW workspace named "boss-ai-monitoring-build" (cwd = the repo;
do NOT pass --env-file — the Claude panes use the existing login, the literal `.env` token is
hook-blocked anyway, and direnv already exports the LangSmith vars into every pane's shell (G14);
the app's own runtime config is read by config.py at RUNTIME). Launch the LEAD as
`claude --dangerously-skip-permissions --model opus[1m]` (I authorize bypass mode; opus for the
lead because coordinating 6 parallel TDD workstreams across a 9-phase spec benefits from the
stronger model). Launch all 6 WORKER panes as
`claude --dangerously-skip-permissions --model sonnet`.

DO NOT run `cmux hooks setup` — `cmux hooks --help` states Claude Code hooks are injected
automatically by the cmux Claude wrapper. Regardless, every agent ALSO fires `cmux notify`
explicitly on each FSM transition and prints a `TASK-DONE:` sentinel, because a semantic
per-transition signal beats a generic turn-stop.

════════════════════════════════════════════════════════════════════════════
BINDING LESSONS — inherited from the macos-ci build lineage; every agent obeys them
════════════════════════════════════════════════════════════════════════════

1. THE PROMPT IS NOT PRIVILEGED OVER THE EVIDENCE. If anything below contradicts a failing test,
   a command you just ran, or the spec HTML itself, the evidence wins. Report the contradiction;
   do not soften what you observed.
2. `cmux send` TYPES; `cmux send-key <ref> enter` SUBMITS; `cmux read-screen --surface <ref>
   --lines 15` CONFIRMS. Three steps, every dispatch. A skipped enter strands the prompt in the
   composer silently — the target agent never runs, never errors, and anyone waiting on its
   sentinel waits forever. One SINGLE-LINE task per send: every newline submits a separate
   prompt. There is no Ctrl-C; to stop a pane, `close-surface` it (scoped — never loop a close
   over the whole tree).
3. CONFIRM BY ARTIFACTS, NOT PIXELS. Progress = `git status --short` deltas, `.team/` file
   mtimes, test/gate exit codes, `duckdb` row counts, `cmux tree --all` pill changes.
   `read-screen --scrollback` has been observed to return only the viewport — never rely on
   scrollback for anything load-bearing. Never match spinner text. SILENCE IS NEVER SUCCESS. A
   Claude Code notification does NOT mean success — it can fire on a turn that refused the work;
   always confirm by artifact.
4. cmux short refs (`surface:N`) are positional and renumber. The stable handles are the window
   UUID and the roster file. Re-resolve surface refs at the moment of use via
   `cmux list-pane-surfaces --workspace <ws>`, and scope every split/send with `--workspace`.
5. Trust `cmux <cmd> --help` over memory; never guess a flag.
6. THIS BUILD LANDS IN THIS REPO. `uv init --package --python 3.13 --name boss-ai-monitoring` at
   the repo root (this yields the `src/boss_ai_monitoring/` module). Do NOT run `gh repo create`
   (the `bossjones/boss-ai-monitoring` remote already exists). Do NOT push — commit locally on the
   current branch; the human pushes/reviews after the run. The repo's existing `specs/`, `prompts/`,
   `.env`, the direnv envrc, `.gitignore`, and `CLAUDE.md` are untouched by the scaffold
   (`CLAUDE.md` is human-owned and read-only this run); the pre-existing root `README.md` is a
   placeholder the lead EXTENDS in place, not a file to replace.
7. THE SPEC HTML IS READ-ONLY REFERENCE — AND YOUR WORKING COPY IS YOUR BRIEF.
   `specs/boss-ai-monitoring/boss-ai-monitoring.html` is the canonical, human-facing spec; it has
   inline `[]`/`[wip]`/`[x]`/`[f]` status markers, but nobody edits them during this run — durable
   phase-tracking lives in `.team/boss-ai-monitoring-build.board.md` instead. One HTML file with
   six agents fighting over its markup is a self-inflicted merge conflict; don't create one.
   For day-to-day reference, each pane reads `specs/boss-ai-monitoring/briefs/shared.md` + its own
   brief (per-pane markdown extractions of the spec, made 2026-07-11, also READ-ONLY this run) —
   they carry your schemas, edge cases, and acceptance criteria without a 61KB HTML read. If a
   brief and the HTML disagree, the HTML (and the evidence) wins — file an OQ; deliberate run
   deviations are flagged inline as `RUN NOTE:` blocks.

SCOPE. This run writes code, runs `uv`, `pytest`, `ruff`, `pyrefly`, `just` recipes, and
`docker compose`, and makes LOCAL git commits at phase boundaries (conventional messages, current
branch). It does NOT: push, run `gh repo create`, switch branches, or delete the user's
`~/.claude/projects` data (mounted read-only wherever it is read, and read-only inside Docker). The
`bossjones/boss-ai-monitoring` remote exists, but this run does not push to it — the human pushes
and reviews after the run, and CI runs on that push. So the `.github/workflows/ci.yml` written in
Phase 1 is validated BY CONSTRUCTION this run (it must exactly mirror `just check`); record its
first real GitHub run as a DEFERRED item on the board, and never fake a CI-green claim.

════════════════════════════════════════════════════════════════════════════
WORKSPACE SETUP — exact recipe
════════════════════════════════════════════════════════════════════════════

PREFLIGHT — verify the whole toolchain BEFORE spawning anyone; a missing tool found now costs
seconds, found at GATE it costs the whole run:

    duckdb --version        # brew install duckdb           (validator's row-count checks)
    langsmith --version     # binary from github.com/langchain-ai/langsmith-cli releases
                            #   (LangSmith read-back verification; installed 2026-07-11)
    just --version && jq --version
    docker info >/dev/null  # daemon RUNNING, not merely installed — start Docker Desktop if not
    direnv exec . sh -c 'test -n "$LANGSMITH_PROJECT"' && echo "langsmith env OK"
                            # presence check ONLY — never echo the values themselves

The last check confirms direnv loads the repo's envrc file (LangSmith auth is AMBIENT: every pane's
shell gets TRACE_TO_LANGSMITH, CC_LANGSMITH_API_KEY, CC_LANGSMITH_PROJECT, LANGSMITH_API_KEY,
LANGSMITH_ENDPOINT, LANGSMITH_WORKSPACE_ID, LANGSMITH_PROJECT automatically — no pane ever handles
a key). If it fails, run `direnv allow` (no path argument — the literal env-file token is
hook-blocked) and re-check. If any tool is missing, STOP and tell the human before spawning panes.

Auto-launch cmux if the socket is down:

    if ! cmux identify --json >/dev/null 2>&1; then
      open -a cmux
      for i in $(seq 1 30); do cmux identify --json >/dev/null 2>&1 && break; sleep 0.5; done
    fi

Reuse the open window (only `new-window` if none exists), then:

    read WS LEAD < <(cmux workspace create --window "$WIN" --name "boss-ai-monitoring-build" \
                       --cwd "$REPO" --focus true --json | jq -r '[.workspace_ref,.surface_ref]|@tsv')
    cmux focus-window --window "$WIN"
    STORE=$(cmux new-split right --workspace "$WS" --surface "$LEAD"  --json | jq -r .surface_ref)  # lead = left half
    OTLP=$(cmux new-split down  --workspace "$WS" --surface "$STORE" --json | jq -r .surface_ref)
    JOBS=$(cmux new-split down  --workspace "$WS" --surface "$OTLP"  --json | jq -r .surface_ref)
    JSONL=$(cmux new-split right --workspace "$WS" --surface "$STORE" --json | jq -r .surface_ref)
    WEB=$(cmux new-split right --workspace "$WS" --surface "$OTLP"  --json | jq -r .surface_ref)
    VALD=$(cmux new-split right --workspace "$WS" --surface "$JOBS" --json | jq -r .surface_ref)

This gives LEAD as the full-height left half, and a 3-row × 2-col grid on the right:
`STORE|JSONL` / `OTLP|WEB` / `JOBS|VALD`.

Identity, so the human can tell everyone apart at a glance:

    cmux rename-tab --workspace "$WS" --surface "$LEAD"  "👑 lead"
    cmux rename-tab --workspace "$WS" --surface "$STORE" "🧱 store"
    cmux rename-tab --workspace "$WS" --surface "$OTLP"  "📡 otlp"
    cmux rename-tab --workspace "$WS" --surface "$JSONL" "📜 jsonl"
    cmux rename-tab --workspace "$WS" --surface "$WEB"   "🖥 web"
    cmux rename-tab --workspace "$WS" --surface "$JOBS"  "⚙️ jobs"
    cmux rename-tab --workspace "$WS" --surface "$VALD"  "✅ validator"
    cmux workspace-action --action set-color --workspace "$WS" --color Teal
    cmux set-status state SCAFFOLD --workspace "$WS" --icon hammer.fill --color "#1565C0"

Persist the roster to `.team/boss-ai-monitoring-build.spawn.json` (window UUID, workspace ref,
role -> surface ref, sentinel `TASK-DONE`). Launch the SIX WORKERS first (type each launch line
INTO its pane via send + send-key enter; kickoff = "You are <role> on team
boss-ai-monitoring-build. Read specs/boss-ai-monitoring/briefs/shared.md then
specs/boss-ai-monitoring/briefs/<role-brief>.md — your spec brief — then
.team/boss-ai-monitoring-build.backlog.md for dispatch. Reply 'ready: <role>' and wait for the
lead."), wait ~6s, then launch the lead with its brief (the lead reads shared.md + lead.md the
same way). Role -> brief file: lead->lead.md, store->store.md, otlp->otlp.md,
jsonl->jsonl-langsmith.md, web->web.md, jobs->jobs.md, validator->validator.md.

════════════════════════════════════════════════════════════════════════════
ROLES AND EXCLUSIVE FILE OWNERSHIP — no file has two writers, ever
════════════════════════════════════════════════════════════════════════════

  👑 lead        pyproject.toml, justfile, .github/workflows/ci.yml, .pre-commit-config.yaml,
                 .env.sample, README.md, LICENSE,
                 src/boss_ai_monitoring/{__init__.py,config.py,cli.py},
                 tests/unit/{test_config.py,test_cli.py}, tests/test_smoke.py, config.sample.yaml,
                 .team/boss-ai-monitoring-build.{board,backlog}.md
  🧱 store       src/boss_ai_monitoring/store/{schema.py,writer.py,views.sql},
                 tests/unit/store/**, tests/fixtures/duckdb/**
  📡 otlp        src/boss_ai_monitoring/ingest/otlp.py, tests/unit/ingest/test_otlp.py,
                 tests/fixtures/otlp/**
  📜 jsonl       src/boss_ai_monitoring/ingest/{jsonl.py,langsmith_poll.py},
                 tests/unit/ingest/{test_jsonl.py,test_langsmith_poll.py},
                 tests/fixtures/{jsonl,langsmith}/**
  🖥 web         src/boss_ai_monitoring/web/** (app.py, templates/*.html, static/style.css),
                 tests/unit/web/**, tests/e2e/test_dashboard.py, docs/AGENT_LOOP.md,
                 docs/design-tokens.md, docs/img/agent-loop/**
  ⚙️ jobs        src/boss_ai_monitoring/jobs/**, notebooks/explore.py, Dockerfile, compose.yaml,
                 tests/unit/jobs/**, tests/integration/test_docker.py
  ✅ validator   .team/boss-ai-monitoring-build.validator-log.md — and NOTHING else by default;
                 any other file only under a lead-issued LOAN TICKET (backlog entry naming the
                 file, the defect, the owner cc'd; ownership returns when the ticket closes)

ONE recorded handoff, at INGEST-FANOUT: 📡 otlp exposes a stable `get_router() -> APIRouter` from
`ingest/otlp.py`; 🖥 web mounts it in `web/app.py` under a lead-issued LOAN TICKET on that one
mount-point block (a marked `# otlp-mount` region) — otlp keeps ownership of the router's
internals, web keeps ownership of the mount call. `store/` is a read/append dependency for every
ingest pane and for jobs (single-writer serialized behind one DuckDB connection owned by
`store/writer.py` — nobody else opens a write connection). Cross-fence needs (e.g. web wants a new
view in `store/views.sql`) go through the backlog, not a direct edit.

The open-questions protocol runs in `.team/boss-ai-monitoring-build.open-questions.md` —
append-only, `OQ-NN` blocks (`Status:` / `Spec:` / `What I tried:` / `Why it is stuck:` /
`My best guess:` / `Cost of guessing wrong:`), file one THE MOMENT you are stuck, notify on first
open. If you catch yourself typing "presumably/it appears/likely/should be", that is an OQ, not a
sentence. An open question is a SUCCESS — especially for the spec's two named RISKs: JSONL format
drift across Claude Code versions, and the LangSmith `thread_id` ↔ `session.id` best-effort join.

════════════════════════════════════════════════════════════════════════════
TDD — non-negotiable, checked by the validator
════════════════════════════════════════════════════════════════════════════

- RED FIRST for every module: the failing test exists and FAILS before the implementation does.
  Your TASK-DONE sentinel carries `red-first-Y/N` and the validator will check the test actually
  fails when the implementation is stubbed out.
- Golden-fixture tests for the SQL views in `store/views.sql` (canned rows in, exact aggregate
  rows out) — do not assert against a live moving dataset.
- `uv run pytest` and `uvx ruff check .` stay green continuously — a red tree blocks everyone, fix
  it before new work.
- The validator independently re-runs every test before accepting any TASK-DONE. A described pass
  is a failure.
- Type checking is **pyrefly** — NOT ty, mypy, or basedpyright. When writing or repairing
  annotations, invoke the `/agent-harness:pyrefly-typing` skill for pyrefly-idiomatic help.

════════════════════════════════════════════════════════════════════════════
STEP ASSIGNMENTS — waves, and the one barrier that matters
════════════════════════════════════════════════════════════════════════════

WAVE 0 — lead, minutes, before dispatching anyone:
  Scaffold in place per Phase 1: `uv init --package --python 3.13 --name boss-ai-monitoring`
  (hatchling backend, src/ layout, MIT LICENSE); `uv add fastapi uvicorn duckdb jinja2 httpx
  langsmith pydantic pydantic-settings pyyaml sse-starlette` + `uv add --dev pytest pytest-asyncio
  respx ruff pyrefly codespell pre-commit playwright`; then `uv run playwright install chromium`
  IMMEDIATELY (the package alone ships no browser — without this the Phase 6 smoke test fails on a
  clean machine). Define the `bam` console script in `pyproject.toml` (`[project.scripts] bam =
  "boss_ai_monitoring.cli:main"` — `bam serve` starts the app; the spec's quickstart and the DONE
  report invoke `uv run bam serve`, so this entrypoint is load-bearing, RED-FIRST test included).
  Write `justfile` (`check` = ruff check + ruff format --check + pyrefly + codespell + pytest,
  `dev`, `fmt`, `docker-build`); write `.github/workflows/ci.yml` + `.pre-commit-config.yaml`;
  write `.env.sample` (CLAUDE_CODE_ENABLE_TELEMETRY, OTEL_METRICS_EXPORTER=otlp,
  OTEL_LOGS_EXPORTER=otlp, OTEL_EXPORTER_OTLP_PROTOCOL=http/json,
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318, TRACE_TO_LANGSMITH, CC_LANGSMITH_API_KEY,
  CC_LANGSMITH_PROJECT, LANGSMITH_API_KEY, LANGSMITH_ENDPOINT, LANGSMITH_WORKSPACE_ID,
  LANGSMITH_PROJECT, BAM_DB_PATH, BAM_CLAUDE_PROJECTS_DIR) — this repo's existing `.env.sample`
  stays and gets extended, not replaced (note the two-key reality: `CC_LANGSMITH_*` feeds the
  Claude Code tracing plugin, `LANGSMITH_API_KEY` feeds the poller and the langsmith CLI; at
  runtime all of them arrive ambiently via direnv). RED-FIRST
  `tests/test_smoke.py::test_package_imports`, then RED-FIRST `config.py` tests (env
  `BAM_`/`__`-nesting > YAML > defaults precedence; missing YAML is fine; malformed YAML raises a
  clear startup error naming the file/line) before implementing `BamSettings` — nested sections
  per the spec: `server` (dashboard port/bind, OTLP port/bind), `store` (db_path), `ingest`
  (jsonl scan interval, claude projects dir, langsmith poll interval + project), `jobs` (per-job
  enable flags). Ship `config.sample.yaml`. Publish `store/writer.py`'s intended interface
  (function signatures + the event dict shape) to the backlog NOW so downstream panes can write
  RED tests against a stub without waiting on store's GREEN. Baseline: `just check` exits 0 —
  paste the output to the board before dispatching anyone.

WAVE 1 — parallel; 🧱 store is the critical path, unblock it first:
  🧱 store (Phase 2): DuckDB schema (`CREATE TABLE IF NOT EXISTS`, idempotent), the `events` table
    (event_id pk, ts, source, event_type, session_id, prompt_id, model, git_sha, agent_name,
    skill_name, tool_name, cost_usd, duration_ms, 4 token-class columns, success, cwd, JSON
    `payload`), an `ingest_cursors` table, a batched Appender writer (buffers N events / T ms,
    atomic flush, idempotent on event_id, single-writer serialized behind one connection), and
    `views.sql` (`v_sessions`, `v_tasks`, `v_costs_daily`, `v_tool_stats`, `v_attribution`,
    `v_five_metrics`) with golden-fixture tests.
  SHADOW WORK while store is in flight (all hermetic, no live DB needed):
    🖥 web scaffolds `templates/*.html` + `static/style.css` (htmx vendored as one static file, no
      npm, no build step) against FIXTURE rows, and stubs `app.py`'s route skeletons.
    ⚙️ jobs scaffolds `jobs/` module skeletons (correction-language scan, drift self-check, error
      classification) with RED-FIRST tests against fixture event rows — no live pipeline needed
      yet.

WAVE 2 — INGEST-FANOUT, once 🧱 store reports GREEN:
  📡 otlp (Phase 3): FastAPI router speaking OTLP http/json ONLY (no gRPC, no otel-collector) on
    `:4318` — `POST /v1/logs`, `POST /v1/metrics`; capture real fixture payloads — the spec names
    six: api_request, tool_result, tool_decision, user_prompt, compaction, api_error, plus one
    metrics export; parse resourceLogs→scopeLogs→logRecords→ObsEvent; idempotency key =
    hash(source, session_id, timeUnixNano, body); malformed input → 400 without crashing; unknown
    event types stored raw in `payload`. Edge cases the spec requires: gzip Content-Encoding
    accepted, oversized bodies rejected at a configurable limit, concurrent posts safe. Publishes
    `get_router()` per the ONE recorded handoff above.
  📜 jsonl (Phase 4 + Phase 5, same pane — both are cursor-based incremental readers with the same
    dedupe discipline): incremental reader over `~/.claude/projects/**/*.jsonl` (mounted
    read-only) with per-file byte-offset cursors (default 15s scan); fixtures cover the spec's
    four transcript shapes (completed session, still-growing session, session with subagents,
    malformed line); extract per-session `git_sha` and `cwd` from transcript metadata; JSONL rows
    dedupe against OTLP keyed on `session_id + request_id` (NOT prompt_id) — JSONL-derived costs
    are ESTIMATES, flagged `source=jsonl` and excluded from cost views when an OTel-derived cost
    exists for the same `session_id + request_id`; truncation/rotation resets the cursor safely;
    edge cases: empty projects dir, thousands of files inside the scan-interval time budget,
    unicode/emoji content, sessions spanning a compaction. Then the LangSmith poller: respx-
    mocked `list_runs(start_time=cursor)`; rate-limit aware (max ~10 req/10s, exponential backoff
    on 429, default 60s poll); graceful degradation with no `LANGSMITH_API_KEY` set; unmatched
    runs land in a visible LangSmith-only bucket, never silently merged into `session.id`. Live
    self-check available to this pane and the validator: `langsmith run list --project
    "$LANGSMITH_PROJECT"` (auth is ambient via direnv) — compare what LangSmith says exists
    against what landed in `events WHERE source='langsmith'`.

WAVE 3 — INTEGRATE, once at least the otlp path is GREEN end-to-end:
  🖥 web (Phase 6): wire the scaffolded templates to the live store — `/` (stat tiles, 14-day
    sparkline, recent sessions), `/live` (SSE feed + active-session cards), `/sessions/{id}`
    (per-task timeline, LangSmith deep-link), `/costs` (rollups + attribution), `/api/events/stream`
    (SSE). Every panel = htmx fragment + JSON twin + a provenance/freshness footer. Playwright
    smoke test. Then (Phase 7) demonstrate ONE real screenshot → critique → edit → reload
    iteration; write `docs/AGENT_LOOP.md` + `docs/design-tokens.md`; save before/after screenshots
    under `docs/img/agent-loop/`. Browser options, in order of preference: Playwright (already a
    dev dependency — scriptable, no extra pane needed) first; if interactive iteration is needed,
    open an 8th `cmux new-surface --type browser` inside this workspace rather than guessing pixel
    coordinates blind.
  ⚙️ jobs (Phase 8): wire the job skeletons to the live `events` table — correction-language scan,
    OTel-vs-JSONL drift self-check (alert badge on divergence), error classification rollup,
    asyncio scheduler with jitter + crash isolation, per-job enable flags from config, last-run
    status persisted. v1 scoring stays deterministic/heuristic — no LLM-judge. NOTE the fence:
    "last-run status shown in the provenance footer" is web-owned rendering of jobs-owned data —
    jobs files a backlog item specifying the query/shape, 🖥 web wires the footer.

WAVE 4 — PACKAGE, once web + otlp are integrated:
  ⚙️ jobs (Phase 9): `uv add --group notebooks marimo`; `notebooks/explore.py` — READ-ONLY DuckDB
    connection (avoids the single-writer conflict with the running app). Multi-stage `Dockerfile`
    + `compose.yaml` — one service, ports 8000 (dashboard) + 4318 (OTLP), a volume for the DuckDB
    file, a READ-ONLY mount for `~/.claude/projects`; document the macOS
    `host.docker.internal` nuance for Claude Code → container OTLP delivery. File a backlog item
    for 👑 lead to fold the README quickstart (`uv run bam serve` → open the dashboard) /
    architecture / LangSmith setup / privacy notes (content-capture OTel flags stay OFF by
    default) into `README.md`, INCLUDING a back-reference link to
    `specs/boss-ai-monitoring/boss-ai-monitoring.html` (the spec requires the README to point back
    at it) — README stays lead-owned, jobs proposes the content via the backlog rather than
    editing it directly. A root `README.md` and `CLAUDE.md` already exist (written 2026-07-11,
    pre-build): the lead EXTENDS the README's placeholder sections in place; `CLAUDE.md` is
    human-owned and READ-ONLY this run — nobody edits it.

════════════════════════════════════════════════════════════════════════════
FSM
════════════════════════════════════════════════════════════════════════════

  SCAFFOLD          (lead: Phase 1 in place, config RED->GREEN, `just check` baseline green)
    -> STORE-CORE   (🧱 store Phase 2 RED->GREEN; SHADOW: 🖥 web + ⚙️ jobs scaffold hermetically)
    -> INGEST-FANOUT (📡 otlp Phase 3, 📜 jsonl Phase 4+5, in parallel, once store is GREEN)
    -> INTEGRATE    (🖥 web Phase 6+7 wired to live events; ⚙️ jobs Phase 8 wired to live events)
    -> PACKAGE       (⚙️ jobs Phase 9: marimo, docker, docs)
    -> GATE          (✅ validator PERSONALLY runs `just check` AND full `uv run pytest -q` AND
        |              the OTLP curl round-trip AND the 3-source DuckDB count AND the LangSmith
        |              read-back cross-check AND `docker compose up --build`; ALL must pass; raw
        |              output PASTED into the board — a described pass is a failure)
        |- CLEAN  -> DONE
        |- DIRTY  -> FIX -> GATE (loop)
        \- ERROR  -> NEEDS-HUMAN

Every agent fires `cmux notify --title "<role>" --body "<state>: <one-line>"` on every transition
of its own, and the first time it opens an OQ. The lead fires the global ones. Tab pills on every
state change (self-rename needs no target flag): `<emoji> <role> <n>/<N> [####------] ·
<one-line log>` with 🔵 working / 🟢 done / 🔴 error / 🟡 fixing-a-defect / ❓ blocked-on-OQ. Lead
mirrors coarse state: `cmux set-status state <STATE> --workspace <ws> --color
<#1565C0 working|#196F3D done|#C0392B error>` and `cmux set-progress <0.0-1.0> --label "<state>"
--workspace <ws>`. Completion sentinel, exact: `TASK-DONE: <role> | <one-line summary> |
tests+N red-first-Y/N`.
The BOARD IS THE DURABLE STATE: any restarted or confused agent is told "read
.team/boss-ai-monitoring-build.board.md and resume from the recorded state." Local git commits at
phase boundaries (SCAFFOLD baseline, STORE-CORE green, INGEST-FANOUT green, INTEGRATE green,
GATE-clean) — lead commits, conventional messages, on the current branch. NEVER push (the
`bossjones/boss-ai-monitoring` remote exists, but the human owns pushing after review).

════════════════════════════════════════════════════════════════════════════
FAILURE HANDLING — roles are fixed; nobody negotiates during an incident
════════════════════════════════════════════════════════════════════════════

TEST/GATE FAILS: the owning pane fixes it (nobody edits another pane's files without a loan
ticket); if it's a cross-cutting defect (e.g. the store schema needs a column no ingest pane can
add itself), file a backlog item naming the owner and the exact change needed — 🧱 store makes the
edit, the requester re-runs its own suite.

WORKER STALLS (no sentinel, stale pill): lead nudges the pane; +5 min, second nudge asking for a
one-line status; still silent -> mark 🔴, move the in-flight ticket to ✅ validator (the standing
substitute) via a loan entry, and re-prompt the stuck pane with "read the board, resume". The board
and backlog are the durable state — no pane restart loses work. Check the stuck pane for a
stranded composer (text sitting above the `❯` line = a send that never got its enter).

DOCKER BUILD SLOW/STALLED (Wave 4): base-image pulls can take a few minutes — that's normal, not a
fault. ⚙️ jobs polls `docker compose up --build` output for actual forward progress (new layer
lines, not silence); if genuinely stuck (no new output for 5+ min AND no process), kill it, check
disk space and Docker Desktop state, retry once, then open an OQ rather than looping silently.

LEAD STALLS: that is the orchestrator's job — see HEARTBEAT.

════════════════════════════════════════════════════════════════════════════
ORCHESTRATOR HEARTBEAT — you (the orchestrator) run this until DONE
════════════════════════════════════════════════════════════════════════════

1. ARM THE DOORBELL once:
     cmux events --name notification.created --no-heartbeat --no-ack --reconnect \
       --cursor-file "$SCRATCH/cmux.seq" > "$SCRATCH/cmux.ev" &
   Poll the FILE (`wc -l` delta + `tail`); a `| jq &` one-liner stalls on stdout buffering.

2. POLL EVERY 60s — one bounded Bash call per cycle (e.g. two 60s ticks per call), each tick
   checking ARTIFACTS: new lines in $SCRATCH/cmux.ev; `git -C "$REPO" status --short | head`;
   mtimes of `.team/boss-ai-monitoring-build.{board,backlog,open-questions}.md`; `cmux tree --all`
   pill states. Liveness = something changed since last tick. Never parse spinner text.

3. REPORT TO THE HUMAN AT LEAST EVERY 2 MINUTES — one line, even when healthy:
     [hh:mm] FSM=<state> pills=<🔵4 🟢2 🔴0> issues=<none | ...>

4. ESCALATE IMMEDIATELY — do not wait for the tick — on: any 🔴/❓ pill; a NEEDS-HUMAN OQ; every
   GATE result. Tell the human what happened, what the team is doing about it, and what (if
   anything) you need from them.

5. THE 3-MINUTE STALL RULE: if NOTHING observable changed for 3 minutes during an active phase,
   probe the LEAD with what you saw from outside ("board mtime frozen 3 min, store pill unchanged,
   composer text visible above the prompt in pane X — did a send miss its enter?"). The lead
   cannot see its workers' composers; you can. Never wait passively. EXCEPTION: `docker compose
   up --build` legitimately goes quiet for a minute or two during layer pulls — that's the jobs
   pane's job to judge, not a stall for you to escalate on its own.

6. If the LEAD itself is unresponsive for ~3 polls: re-prompt it with "read
   .team/boss-ai-monitoring-build.board.md and resume the FSM from the recorded state." Nothing
   depends on the lead's memory.

════════════════════════════════════════════════════════════════════════════
GROUND TRUTHS — load-bearing facts from the spec; do not re-derive or re-litigate them
════════════════════════════════════════════════════════════════════════════

G1.  OTLP is http/json ONLY — no gRPC, no otel-collector in the path. The FastAPI app speaks OTLP
     directly.
G2.  Ports: 8000 = dashboard, 4318 = OTLP receiver.
G3.  Type checker = pyrefly (NOT ty/mypy/basedpyright). Invoke `/agent-harness:pyrefly-typing` for
     annotation help.
G4.  Config precedence: env vars (prefix `BAM_`, nested via `__`) > YAML file (`config.yaml` or
     `$BAM_CONFIG`) > code defaults. No ad-hoc `os.environ` reads anywhere outside `config.py`.
G5.  Single DuckDB file; canonical event envelope with a JSON `payload` column (no migrations as
     event types evolve); batched Appender writer, idempotent on `event_id`, single-writer
     serialized behind one connection.
G6.  JSONL-derived costs are ESTIMATES — flagged, and excluded from cost views whenever an
     OTel-derived cost exists for the same `session_id + request_id` (the spec's dedupe key —
     not prompt_id).
G7.  LangSmith rate limit ~10 req/10s on ≤7-day windows -> cursor polling only, never full scans;
     the poller degrades gracefully with no API key configured.
G8.  OTel content-capture flags (`OTEL_LOG_USER_PROMPTS` / `_ASSISTANT_RESPONSES` / `_TOOL_DETAILS`)
     stay OFF by default — opt-in only.
G9.  v1 quality scoring is deterministic/heuristic — no LLM-judge (Anthropic's own warning:
     LLM-generated definitions "encoded the very ambiguities we were trying to eliminate").
G10. Dashboard and OTLP ports bind localhost by default, NO AUTH — single-user, single-host,
     trusted-network scope. Multi-user aggregation is explicitly out of scope.
G11. Frontend htmx is vendored as a single static file — no npm, no build step.
G12. This build lands IN this repo. Commit locally on the current branch — no `gh repo create`
     (the `bossjones/boss-ai-monitoring` remote already exists), and do NOT push; the human owns
     pushing to the remote after review.
G13. TEST LAYOUT: this prompt's ownership map (`tests/unit/**`, `tests/integration/**`,
     `tests/e2e/**`, filenames `test_dashboard.py` / `test_langsmith_poll.py`) is AUTHORITATIVE
     over the spec's literal pytest paths (`tests/store`, `tests/ingest/test_langsmith.py`,
     `tests/e2e/test_smoke_playwright.py`). Where a spec validation command names a path, run the
     equivalent suite under this layout — do not create parallel directories to satisfy the spec's
     wording.
G14. LangSmith auth is AMBIENT: direnv exports TRACE_TO_LANGSMITH, CC_LANGSMITH_API_KEY,
     CC_LANGSMITH_PROJECT, LANGSMITH_API_KEY, LANGSMITH_ENDPOINT, LANGSMITH_WORKSPACE_ID, and
     LANGSMITH_PROJECT into every pane's shell. The `langsmith` CLI (v0.2.39, preinstalled,
     verified working against the live `boss-ai-monitoring` project) is the read-back tool:
     `langsmith run list --project "$LANGSMITH_PROJECT"`, `langsmith trace get`, `langsmith
     thread list`. Never print any of these env values; never open or read the envrc file itself.

════════════════════════════════════════════════════════════════════════════
GOTCHAS — inherited from the macos-ci build lineage plus boss-cmux-skill specifics
════════════════════════════════════════════════════════════════════════════

- The pre_tool_use hook blocks the substrings `rm `, `.env`, and `--rm`. Use `mv` into the
  scratchpad instead of `rm`; never type the literal `.env` token (hence no `--env-file` on
  workspace create).
- NEVER read secret values (`cat .env`, `echo $KEY`) even though nothing was injected via
  `--env-file` this run — the repo's `.env` exists for the app's own runtime, not for you to
  print. The same applies to the direnv envrc file (gitignored, holds the LangSmith keys): never
  open it, never echo its variables — its whole point is that auth is ambient and invisible.
- direnv blocks the envrc file again after ANY edit to it. Symptom: `langsmith` suddenly
  unauthenticated or `$LANGSMITH_PROJECT` empty. Fix: `direnv allow` — bare, no path argument
  (the path contains the hook-blocked literal token). Observed live on 2026-07-11.
- Bash is auto-rewritten through rtk. zsh does not word-split unquoted vars — inline lists or
  `${=var}`.
- Lint with `uvx ruff check <file>`; type-check via `uv run pyrefly check` (not bare `pyrefly`
  unless it's on PATH); Python runs via `uv run`.
- `docker compose` on macOS needs `host.docker.internal` for the containerized app to receive
  OTLP from a host-side Claude Code session.
- `~/.claude/projects` is mounted READ-ONLY wherever it is read — locally by the jsonl reader,
  and read-only inside the Docker container.
- `just check` is the only definition of done — a broken lint/type/test gate blocks GATE
  regardless of how much feature work is finished.
- The spec HTML's inline status markers are cosmetic reference only this run (see BINDING LESSON
  7) — don't spend a turn editing them; spend it on `.team/boss-ai-monitoring-build.board.md`.

════════════════════════════════════════════════════════════════════════════
DONE REPORT — lead compiles, orchestrator relays to the human, in this order
════════════════════════════════════════════════════════════════════════════

  a. `just check` output (ruff + pyrefly + codespell + full pytest), pasted, zero warnings.
  b. `uv run pytest -q` summary — every TDD suite (store, ingest×3, web, jobs, e2e) green, plus
     red-first counts per pane.
  c. OTLP curl round-trip: `curl -sf -X POST localhost:4318/v1/logs -d @tests/fixtures/otlp/api_request.json
     -H 'Content-Type: application/json'` then reload `/` — fixture event visible, pasted.
  d. `duckdb $BAM_DB_PATH "SELECT source, count(*) FROM events GROUP BY 1"` — otlp, jsonl, and
     langsmith all > 0, pasted. Generate the live data like this (one command covers all three
     sources): with the app running, the validator launches ONE headless session —
     `claude -p "say hi" ` with CLAUDE_CODE_ENABLE_TELEMETRY=1 and the OTEL_* vars from
     `.env.sample` pointed at localhost:4318 — which emits real OTLP events AND writes a real
     `~/.claude/projects` JSONL transcript; the LangSmith tracing plugin (TRACE_TO_LANGSMITH,
     ambient) covers the third source on the poller's next cycle.
  d2. LangSmith read-back cross-check: `langsmith run list --project "$LANGSMITH_PROJECT"
     --limit 10` output pasted next to `duckdb $BAM_DB_PATH "SELECT count(*) FROM events WHERE
     source='langsmith'"` — the runs LangSmith reports and the rows we ingested must be
     consistent (unmatched runs allowed only in the visible LangSmith-only bucket).
  e. `docker compose up --build` round-trip result, pasted.
  f. `uvx marimo run notebooks/explore.py` opens read-only against a live DB without errors.
  g. Phase 7 agent-loop artifacts: `docs/AGENT_LOOP.md` + before/after screenshots under
     `docs/img/agent-loop/`, confirmed present.
  h. Deferred items (the un-run-on-GitHub CI workflow, and anything else on the board); open
     questions, NEEDS-HUMAN first; roster table with final pill states and the phase-boundary
     commit list.

Report back when the team is up with the roster (pane -> role -> surface) and the board rendered.
Then start the heartbeat and do not stop until DONE or NEEDS-HUMAN.
````

---

## After the run

```bash
cd /Users/bossjones/dev/bossjones/boss-ai-monitoring
just check && uv run pytest -q && echo "trustworthy"
duckdb "$BAM_DB_PATH" "SELECT source, count(*) FROM events GROUP BY 1"   # otlp/jsonl/langsmith all > 0
langsmith run list --project "$LANGSMITH_PROJECT" --limit 10             # read-back: LangSmith agrees with what we ingested
docker compose up --build -d && curl -sf localhost:8000/ && docker compose down
git log --oneline -8   # phase-boundary commits, all local, nothing pushed
git status --short     # confirm working tree matches what the board claims
```

Read `.team/boss-ai-monitoring-build.open-questions.md` first — the `NEEDS-HUMAN` entries are
yours. The build team committed locally only; push the build branch to `origin`
(`bossjones/boss-ai-monitoring`) when you're ready and `gh run watch` the first real CI run (the
deferred item on the board). The natural successor to this prompt is a small verify-style run over
whatever `<!-- UNVERIFIED -->`-equivalent gaps the build leaves behind (start with the two named
spec RISKs: JSONL format drift and the LangSmith join).
