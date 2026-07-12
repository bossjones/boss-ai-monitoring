# Plan: VCR-backed LangSmith integration tests

## Task Description

Add integration tests for the LangSmith poller (`ingest/langsmith_poll.py`) that exercise the
**real** `api.smith.langchain.com` wire protocol via recorded vcrpy cassettes, modeled on how
`langchain-ai/langsmith-sdk` (cloned at `/Users/bossjones/dev/langchain-ai/langsmith-sdk`) sets up
VCR: their conftest patterns, cassette generation flow, and — critically — what they scrub before
anything is written to disk so no secrets leak into committed cassettes.

Motivation: the 2026-07-12 `limit=200 → 400 Bad Request` bug shipped green because every existing
test mocks LangSmith at the HTTP layer with *compliant, hand-built* responses
(`tests/unit/ingest/conftest.py` respx fixtures). A respx mock can never disagree with us; a
cassette recorded from the live API can. Recording is the moment of truth (a 400 fails the
recording run), and replay pins the real wire shape into CI forever.

## Objective

- `tests/integration/langsmith/` suite: 3 async tests driving `poll_once` end-to-end against
  cassettes recorded from the real LangSmith API.
- Own pytest marker (`langsmith_vcr`) so the suite can be selected/deselected independently.
- **Replay runs everywhere** (CI + `just check`, hermetic, zero network, zero secrets).
  **Recording is local-only** (needs the direnv-ambient `LANGSMITH_API_KEY`).
- Committed cassettes are provably secret-free: header stripping + body scrubbing at record time,
  plus a hygiene test that scans every cassette on every run.

## Problem Statement

Unit tests stub the client, so the suite cannot catch contract drift between our poller and the
real LangSmith API (page-size caps, response envelope changes, pagination cursor shape, auth
header semantics). We need tests that replay real recorded traffic — without committing the
`lsv2_...` API key, tenant identifiers, or gzip-opaque blobs into git.

## Solution Approach

Adapt the langsmith-sdk pattern (`python/tests/integration_tests/conftest.py` is the canonical
template; `python/conftest.py` is the doctest variant) with **one deliberate inversion**:

| Concern | langsmith-sdk does | We do |
|---|---|---|
| Host recording policy | Allowlist **only** `api.openai.com` / `api.anthropic.com` / Gemini; `before_record_request` returns `None` for everything else — so LangSmith's own API is *never* recorded (their tests hit it live) | Invert: allowlist **only** `api.smith.langchain.com`; drop everything else. Same mechanism, opposite target |
| Header scrubbing | `filter_headers=["Authorization", "X-Api-Key", "x-goog-api-key", "OpenAI-Organization", "Anthropic-Version", "User-Agent", ...]` | Same list trimmed to what LangSmith traffic carries: `x-api-key` (the LangSmith auth header), `Authorization`, `User-Agent` (version churn + fingerprint), plus response `Set-Cookie` |
| Body scrubbing | `before_record_request` regex: `(api[-_]?key"?\s*:\s*")\w+(")` → `FILTERED`, `(Bearer\s+)[\w-]+` → `FILTERED` | Same two regexes, plus `lsv2_[A-Za-z0-9_]+` → `lsv2_FILTERED` (LangSmith key format) and tenant/org UUID normalization in `/sessions` responses |
| Readability | `decode_compressed_response=True` | Same — **mandatory**: one older SDK cassette (`python/tests/cassettes/58e9f031....yaml`) shows what happens without it: the response body is a `!!binary` gzip blob that no grep/reviewer can audit. Undecodable body = unauditable body |
| Record mode | pytest CLI opt `--vcr-mode`, default `once` | Same opt, but default **`none`** (replay-only; a missing/mismatched cassette is a loud error, never a silent live call). Recording is an explicit local `--vcr-mode=all` |
| Cassette naming | `{module}_{function}.yaml` via an autouse function-scoped fixture | Same |
| Selection | `slow` marker + `--runslow` skip in `pytest_collection_modifyitems` | Repo already has this idiom: `docker` marker + `addopts = -m "not docker"` (pyproject.toml:62-65, `just test-docker`). We add a `langsmith_vcr` marker but do NOT deselect it by default — replay is hermetic and belongs in `just check`/CI |

Not carried over: `patch_vcr_aiohttp()` / the urllib3 `ConnectionRemover` monkeypatch
(`langsmith/_internal/_patch.py`) — those work around the SDK's aiohttp/urllib3 paths under
concurrent VCR use. Our poller only exercises `AsyncClient` → httpx, which vcrpy ≥ 7 supports
natively. Their custom md5 body matcher is also skipped — it exists for drifting OpenAI payloads;
we instead make our own request bodies deterministic (see cursor seeding below).

**Determinism (the one real design problem):** on a cursorless first poll, `poll_once` computes
`start_time = datetime.now(UTC) - 7 days` — a body that changes every run. Fix: every VCR test
seeds a fixed cursor first (`writer.set_cursor("langsmith", PROJECT, "<fixed ISO ts>")`), so the
`/runs/query` body is byte-stable across record and replay. Match on vcrpy's default
(`method, scheme, host, port, path, query` — no body) for resilience to harmless SDK body-key
reordering; sequential same-URI POSTs (pagination) replay in recorded order.

**Auth in the two modes:**
- Replay (`--vcr-mode=none`, the default): conftest monkeypatches `LANGSMITH_API_KEY` to
  `lsv2_pt_vcr-fake-key` — satisfies `poll_once`'s `client.api_key` presence check, works with no
  direnv, and *cannot* accidentally hit the network (mode `none` forbids it).
- Record (`--vcr-mode=all`): requires the real ambient key; `pytest.skip` with a clear message if
  `LANGSMITH_API_KEY` is absent (presence check only — never echo it, per CLAUDE.md).

## Relevant Files

Study/reference (read-only, in the cloned SDK):
- `langsmith-sdk/python/tests/integration_tests/conftest.py` — the template: VCR instance factory,
  host allowlist, header/body filters, autouse cassette fixture, `--vcr-mode`/`--runslow`
- `langsmith-sdk/python/conftest.py` — doctest variant; shows `pytest_addoption` lives in the
  rootdir-level conftest
- `langsmith-sdk/python/pyproject.toml` — `vcrpy>=7.0.0` dep placement (dev group + `vcr` extra),
  `markers = ["slow: ..."]`
- `langsmith-sdk/python/tests/cassettes/*.yaml` — real cassette anatomy; verified 0 grep hits for
  `x-api-key|lsv2_|Bearer` (their scrubbing works), and the `!!binary` counterexample

Ours to modify:
- `pyproject.toml` — add `vcrpy>=7.0.0` to `[dependency-groups].dev` (via `uv add --group dev`);
  add `langsmith_vcr` marker to `[tool.pytest.ini_options].markers`
- `tests/conftest.py` — add `pytest_addoption` for `--vcr-mode` (must live in an initial conftest;
  `testpaths = ["tests"]` makes this one load at startup). NOTE this file's docstring declares
  lead-pane ownership — single-agent build may edit it, but keep the addition minimal (one hook)
- `justfile` — `test-vcr` and `record-vcr` recipes
- `src/boss_ai_monitoring/ingest/langsmith_poll.py` — NOT modified; it is the system under test

Reused as-is:
- `tests/conftest.py::db_path` fixture (tmp DuckDB path)
- `EventWriter` context-manager usage pattern from `tests/unit/ingest/test_langsmith_poll.py`
- The `docker`-marker precedent (`tests/integration/test_docker.py`, pyproject addopts,
  `just test-docker`) for marker declaration and justfile wiring

### New Files
- `tests/integration/langsmith/__init__.py`
- `tests/integration/langsmith/conftest.py` — VCR factory + autouse cassette fixture + auth-mode
  fixture (adapted SDK template, inverted host allowlist)
- `tests/integration/langsmith/test_langsmith_poll_vcr.py` — the 3 tests, `pytestmark = pytest.mark.langsmith_vcr`
- `tests/integration/langsmith/test_cassette_hygiene.py` — secret-scan guard (NO marker: runs in
  every suite invocation, it's milliseconds of file I/O)
- `tests/integration/langsmith/cassettes/*.yaml` — committed, scrubbed recordings

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom. TDD red-first applies: steps 3–5 produce
failing tests; steps 6–7 make them green.

### 1. Dependency + markers
- `uv add --group dev "vcrpy>=7.0.0"`
- In `pyproject.toml` markers list add:
  `"langsmith_vcr: replays recorded LangSmith API cassettes; hermetic, runs by default; record locally via just record-vcr"`
- Leave `addopts = "-ra -m \"not docker\""` unchanged (replay tests are meant to run by default).

### 2. `--vcr-mode` option in `tests/conftest.py`
- Append a `pytest_addoption` hook: `--vcr-mode`, type str, default `"none"`, help text naming the
  three sanctioned values (`none` = replay-only, `once` = record-if-missing, `all` = re-record).
- Nothing else in the shared conftest changes.

### 3. Cassette hygiene test (write first — it also documents the scrubbing contract)
- `test_cassette_hygiene.py`: glob `tests/integration/langsmith/cassettes/*.yaml`; for each file
  assert the text contains NONE of: `lsv2_` not followed by `FILTERED`/`pt_vcr-fake`,
  `Bearer [A-Za-z0-9]`, an `x-api-key` header with a non-`FILTERED` value, `!!binary` (an
  undecoded body would be unauditable), and — belt-and-braces — the literal value of
  `os.environ["LANGSMITH_API_KEY"]` when that var is set (compare in memory, never print).
- Passes vacuously on an empty cassette dir (so it's green before recording ever happens).

### 4. `tests/integration/langsmith/conftest.py`
- Port the SDK's `create_vcr_instance` shape with our policy:
  - `cassette_library_dir` = `tests/integration/langsmith/cassettes`
  - `record_mode` from `--vcr-mode`
  - `filter_headers = ["x-api-key", "X-Api-Key", "Authorization", "User-Agent"]`
  - `before_record_request`: return `None` unless `request.host == "api.smith.langchain.com"`
    (inverted allowlist); then regex-scrub body for `lsv2_[A-Za-z0-9_]+`, `Bearer \S+`, and the
    SDK's `api[-_]?key` JSON pattern
  - `before_record_response`: scrub `Set-Cookie`; normalize `tenant_id`/`id` of the tenant in
    `/sessions` bodies to a fixed UUID string; apply the same body regexes
  - `decode_compressed_response=True` (non-negotiable — see hygiene test's `!!binary` assertion)
  - default `match_on` (no body)
- Autouse function-scoped `vcr_cassette` fixture (SDK style): cassette name
  `f"{module}_{function}.yaml"`, wraps the test in `use_cassette`.
- `langsmith_auth` autouse fixture: if `--vcr-mode == "none"` monkeypatch
  `LANGSMITH_API_KEY=lsv2_pt_vcr-fake-key`; else `pytest.skip` unless the real var is present
  (presence check only). Scope both fixtures to this directory (they live in this conftest, so
  they cannot leak into unit tests — and the unit dir's own `isolated_langsmith_env` still guards
  the other direction).

### 5. The three tests (`test_langsmith_poll_vcr.py`) — red until cassettes exist
- All: `pytestmark = pytest.mark.langsmith_vcr`; construct `AsyncClient()` with **no explicit
  api_url/key** (ambient/monkeypatched env, exactly like production); reuse `db_path` +
  `EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000)`; seed
  `writer.set_cursor("langsmith", PROJECT, FIXED_TS)` before polling; `PROJECT =
  "boss-ai-monitoring"` and `FIXED_TS` chosen at record time to cover a window with known runs.
- `test_poll_once_against_live_recording`: one `poll_once` pass → `status == "ok"`,
  `runs_seen > 0`, rows land in DuckDB with `source='langsmith'`, `event_id` prefixed
  `langsmith:`, cursor advanced beyond `FIXED_TS`.
- `test_poll_once_paginates_past_100_runs`: `poll_once(..., limit=250)` over a window recorded to
  contain >100 runs → `runs_seen > 100`. This is the live-shaped regression for the limit bug:
  re-recording with an unclamped limit would 400 and fail the recording run, and the cassette
  itself proves the API accepted our clamped page size while cursor-chaining pages.
- `test_second_pass_is_idempotent_and_cursor_driven`: two sequential `poll_once` calls inside one
  cassette → second pass's `events_written` produces no duplicate `event_id`s in the DB (writer
  dedupe + `_CURSOR_OVERLAP` re-fetch is a no-op), total row count equals distinct run count.

### 6. Record cassettes (local, direnv shell)
- `direnv exec . uv run pytest tests/integration/langsmith -m langsmith_vcr --vcr-mode=all -v`
- Pick `FIXED_TS` values that make the assertions true against the real `boss-ai-monitoring`
  project (>100 runs exist as of 2026-07-12; the poller wrote 150 events today).
- **Human review gate before committing cassettes**: read the YAML end-to-end once; run the
  hygiene test; additionally `grep -c "FILTERED" cassettes/*.yaml` to confirm scrubbing actually
  fired (0 hits on the auth header pattern + present FILTERED markers = scrubbed, not merely
  absent).

### 7. Prove replay is hermetic, then wire justfile
- `env -u LANGSMITH_API_KEY -u LANGSMITH_PROJECT uv run pytest tests/integration/langsmith -v`
  must pass with network access irrelevant (mode `none` + fake key).
- justfile: `test-vcr: uv run pytest -m langsmith_vcr -v tests/integration/langsmith` and
  `record-vcr: direnv exec . uv run pytest tests/integration/langsmith --vcr-mode=all -v && uv run pytest tests/integration/langsmith/test_cassette_hygiene.py -v`

### 8. Validate everything
- Run the Validation Commands below; `just check` must stay green (251 existing + new tests).
- Commit locally (cassettes included) on the current branch; do not push.

## Testing Strategy

The suite is itself the test infrastructure; its own risks and mitigations:
- **Non-determinism** → fixed cursor seeding makes request bodies stable; default matcher ignores
  body anyway; no wall-clock assertions (assert relative ordering, not absolute timestamps).
- **Secret leakage** → three layers: record-time filters (headers, body regex, host allowlist),
  always-on hygiene test, human review gate at step 6.
- **False hermeticity** (test secretly hits network) → default mode `none` raises
  `CannotOverwriteExistingCassetteException` on any unmatched request; step 7's `env -u` run
  proves it.
- **vcrpy/httpx interplay** → vcrpy ≥ 7 patches `httpx.AsyncClient` at class level, so client
  construction order vs. cassette entry doesn't matter; if the langsmith SDK ever switches
  transports (they patch aiohttp for a reason), the recording run fails loudly — acceptable.

## Acceptance Criteria

- [ ] `uv run pytest -m langsmith_vcr` passes with **no** LangSmith env vars and no network.
- [ ] `uv run pytest -m "not langsmith_vcr"` cleanly deselects the suite.
- [ ] `just record-vcr` re-records against the live API in a direnv shell (and would have failed
      red on the historical `limit=200` bug).
- [ ] `grep -rE "lsv2_[A-Za-z0-9_]+|Bearer [A-Za-z0-9]" tests/integration/langsmith/cassettes/ | grep -vE "lsv2_(FILTERED|pt_vcr)"`
      finds nothing; hygiene test enforces the same in-suite, including no `!!binary` bodies.
- [ ] `just check` green; new marker documented in pyproject; cassettes committed and
      human-reviewed.
- [ ] No changes to `src/` (tests-only change).

## Validation Commands

- `uv run pytest tests/integration/langsmith -v` — full new suite (replay mode)
- `env -u LANGSMITH_API_KEY -u LANGSMITH_PROJECT uv run pytest -m langsmith_vcr -v` — hermeticity proof
- `uv run pytest -m "not langsmith_vcr" -q` — deselection works
- `uv run pytest tests/integration/langsmith/test_cassette_hygiene.py -v` — secret scan
- `just check` — definition of done (ruff, pyrefly, codespell, full pytest)

## Notes

- New dependency: `uv add --group dev "vcrpy>=7.0.0"` (matches the SDK's floor; they also publish
  a `vcr` extra but we only need the dev-group dep).
- Do **not** adopt `pytest-recording`/`pytest-vcr` plugins — the SDK uses raw `vcr.VCR` and so do
  we; fewer layers between us and the scrubbing hooks.
- Cassette payloads still contain run *metadata* (run names, model names, token counts, project
  name). Our poller's `_SELECT_FIELDS` deliberately excludes `inputs`/`outputs`, so no prompt or
  completion content is ever in a recording — this is worth stating in the conftest docstring as
  the reason the select-list matters for privacy, not just bandwidth (ties into the repo's
  content-capture-OFF privacy stance).
- The `rm `-blocking pre_tool_use hook applies to re-recording: to discard a stale cassette, `mv`
  it to the scratchpad instead of deleting.
- If LangSmith rate-limits during recording (~10 req/10s budget), take the SDK's
  `skip_if_rate_limited` decorator idea only if it actually bites; don't add it speculatively.
