# jsonl-langsmith.md — 📜 jsonl brief (Phase 4: JSONL Backfill + Phase 5: LangSmith Poller)

> Derived from `../boss-ai-monitoring.html` (canonical) on 2026-07-11. Read `shared.md` first.
> If this conflicts with the HTML or observed behavior, the evidence wins — file an OQ.

**You own:** `src/boss_ai_monitoring/ingest/{jsonl.py,langsmith_poll.py}`,
`tests/unit/ingest/{test_jsonl.py,test_langsmith_poll.py}`, `tests/fixtures/{jsonl,langsmith}/**`.
Both phases are cursor-based incremental readers with the same dedupe discipline — that's why
they share a pane.

---

## Phase 4: JSONL Transcript Backfill + Gap-Fill

OTel is forward-only; transcripts are the historical record and the safety net when the endpoint
was down. Incremental reader over `~/.claude/projects/` with per-file byte-offset cursors
(agentic-metric's proven pattern), deduped against OTLP-sourced events. The directory is
**read-only wherever it is read** — locally and inside Docker.

### 1. Incremental reader (TDD)

- **Fixtures first:** commit sanitized fixture transcripts covering, exactly: a completed
  session, a still-growing session, a session with subagents, a malformed line.
  (Spec names `tests/fixtures/transcripts/`; this run's ownership map puts them under
  `tests/fixtures/jsonl/` — same content, run layout wins.)
- RED: scan discovers files; parses assistant/user/tool entries into ObsEvents
  (`source=jsonl`); a second scan after appending lines ingests ONLY the delta (byte-offset
  cursor persisted in `ingest_cursors`); truncated/rotated file resets its cursor safely;
  malformed lines are skipped with a logged warning, never a crash.
- GREEN: `ingest/jsonl.py` — polling scanner (configurable interval, default 15s) + parser.
- RED→GREEN dedupe: when an OTLP event with the same **`session_id + request_id`** already
  exists, the JSONL row is marked `source=jsonl` shadow and **excluded from cost views** (OTel
  cost is authoritative; JSONL costs are estimates and flagged as such).
- RED→GREEN: per-session **`git_sha`/`cwd` extraction** from transcript metadata where present.

### Phase 4 testing strategy

Pure pytest over fixture directories in `tmp_path` — hermetic. Edge cases the spec requires:

- empty projects dir
- thousands-of-files dir (scan stays under a time budget)
- unicode/emoji content
- sessions spanning a compaction

### Phase 4 acceptance

- `uv run pytest tests/unit/ingest/test_jsonl.py -q` — green incl. cursor, dedupe,
  malformed-line cases.
- Manual E2E (validator re-runs): point at real `~/.claude/projects/` → historical sessions
  appear; re-run ingests nothing new (cursors hold).
- `just check` — clean.

---

## Phase 5: LangSmith Poller (merged view)

The langsmith-claude-code-plugins hook plugin already pushes traces up; this phase pulls them
back down. LangSmith is **enrichment, not a dependency**.

### 1. Poller (TDD)

- RED (respx-mocked LangSmith API): poller calls
  `list_runs(project_name=$CC_LANGSMITH_PROJECT, start_time=<cursor>, select=[trimmed fields])`;
  maps runs → ObsEvents (`source=langsmith`; run_type, latency, token usage, feedback,
  trace_id); persists the start_time cursor; join populates `session_id` from `thread_id`.
- RED→GREEN rate-limit awareness: max 10 req/10s budget, exponential backoff on 429, poll
  interval configurable (default 60s).
- RED→GREEN graceful degradation: missing/invalid `LANGSMITH_API_KEY` disables the poller with a
  visible dashboard notice, never a crash.
- RED→GREEN unmatched runs (no local session with that thread_id): stored and surfaced in a
  "LangSmith-only" bucket, never dropped, never silently merged (timestamp-proximity is a
  secondary hint only).

### Phase 5 testing strategy

respx-mocked httpx transport — hermetic, deterministic. Edge cases the spec requires:

- empty pages; pagination
- 429 storms
- runs with missing token usage
- clock-skewed start_time overlap (cursor overlaps 1 min and dedupes on run id)

### Phase 5 acceptance

- `uv run pytest tests/unit/ingest/test_langsmith_poll.py -q` — green incl. rate-limit and
  degradation cases.
- Manual E2E with the real key (ambient via direnv): one traced Claude Code session appears in
  DuckDB with both `otlp` and `langsmith` rows sharing a `session_id`.
- `just check` — clean.

### Live self-check (you and the validator)

`langsmith run list --project "$LANGSMITH_PROJECT"` — compare what LangSmith says exists against
`duckdb $BAM_DB_PATH "SELECT count(*) FROM events WHERE source='langsmith'"`. Auth is ambient;
never print the env values. The CLI also has `trace get` / `thread list` for digging into a
specific join question. If the `thread_id` ↔ `session.id` join misbehaves, that is the spec's
named RISK #2 — file an OQ immediately with what you observed.
