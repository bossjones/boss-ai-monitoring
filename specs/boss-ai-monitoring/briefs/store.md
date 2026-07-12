# store.md — 🧱 store brief (Phase 2: Storage Core — DuckDB Schema, Writer, Metric Views)

> Derived from `../boss-ai-monitoring.html` (canonical) on 2026-07-11. Read `shared.md` first.
> If this conflicts with the HTML or observed behavior, the evidence wins — file an OQ.

**You own:** `src/boss_ai_monitoring/store/{schema.py,writer.py,views.sql}`,
`tests/unit/store/**`, `tests/fixtures/duckdb/**`. You are the critical path — Wave 2 cannot
start until you report GREEN.

One DuckDB file is the whole persistence story. A canonical `ObsEvent` envelope
(pi-agent-observability pattern) with a JSON payload column means new event types need no
migrations; SQL views turn raw events into every metric the dashboard shows.

## 1. Event schema (TDD)

- RED: tests for the `events` table shape — columns, exactly:
  `event_id` (pk), `ts`, `source` (otlp|jsonl|langsmith), `event_type`, `session_id`,
  `prompt_id`, `request_id` (nullable — the API request identifier; keys the JSONL↔OTel
  dedupe/cost exclusion), `model`, `git_sha`, `agent_name`, `skill_name`, `tool_name`,
  `cost_usd`, `duration_ms`, `tokens_input`, `tokens_output`, `tokens_cache_read`,
  `tokens_cache_creation`, `success`, `cwd`, `payload` (JSON).
- Plus `ingest_cursors` table: `source`, `key`, `cursor`, `updated_at`.
- GREEN: `store/schema.py` creates tables idempotently (`CREATE TABLE IF NOT EXISTS`).

## 2. Batched writer (TDD)

- RED: writer buffers N events / T ms then flushes atomically via DuckDB Appender; duplicate
  `event_id` is a no-op (idempotent ingest); single-writer access serialized behind ONE
  connection — nobody else ever opens a write connection.
- GREEN: `store/writer.py` with async-safe batch queue + flush loop.

## 3. Metric views (TDD — golden fixtures)

- RED: seed a fixture event set with KNOWN totals; assert exact view outputs. Never assert
  against a live moving dataset.
- Views, exactly six:
  - `v_sessions` — start/end/duration/cost/model per session
  - `v_tasks` — per `prompt_id`: wall-clock, cost, tokens, tool counts
  - `v_costs_daily`
  - `v_tool_stats` — success rate, p50/p95 duration per tool
  - `v_attribution` — cost + latency per agent_name/skill_name/model (the Anthropic per-feature
    attribution view)
  - `v_five_metrics` — task completion rate; tool selection accuracy via `tool_result.success`;
    autonomy score via `tool_decision.source`; recovery rate via error→retry patterns; cost per
    successful task
- **Cost-exclusion rule (you own it — it lives in `views.sql`):** every cost-bearing view
  (`v_costs_daily`, and the cost fields of `v_tasks`, `v_attribution`, `v_five_metrics`)
  EXCLUDES `source='jsonl'` cost rows whenever an OTel-derived cost exists for the same
  `session_id + request_id` — JSONL costs are flagged estimates, OTel cost is authoritative (G6).
  Golden-fixture test: a fixture set containing the same request via both sources must count the
  cost exactly once.
- GREEN: `store/views.sql` applied at startup.

## Testing strategy

Pure pytest against a temp-file DuckDB — embedded, no mocks. Edge cases the spec requires:

- empty DB
- duplicate event ids
- events missing optional attrs (NULL `prompt_id`)
- out-of-order timestamps
- JSON payload with unexpected keys (must not break views)

## Acceptance

- `uv run pytest tests/unit/store -q` — schema, writer, and all six views green incl. edge cases.
- `just check` — zero lint/type errors.
- Red-first evidence per module in your TASK-DONE sentinel.

## Notes

- Downstream panes write RED tests against the writer interface the lead published to the
  backlog — if you need to change a signature after that, it's a backlog item, not a silent edit.
- Cross-fence view requests (e.g. web wants a new view) come to you via the backlog; you make
  the edit in `views.sql`, requester re-runs their suite.
- Documented fallback if ingest rate ever demands it (do NOT build now): SQLite-WAL staging with
  batched flushes into DuckDB.
