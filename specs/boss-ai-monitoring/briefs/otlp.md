# otlp.md — 📡 otlp brief (Phase 3: Ingest — OTLP Receiver, live + authoritative)

> Derived from `../boss-ai-monitoring.html` (canonical) on 2026-07-11. Read `shared.md` first.
> If this conflicts with the HTML or observed behavior, the evidence wins — file an OQ.

**You own:** `src/boss_ai_monitoring/ingest/otlp.py`, `tests/unit/ingest/test_otlp.py`,
`tests/fixtures/otlp/**`.

Skip the otel-collector: the FastAPI app itself speaks OTLP http/json on `:4318`; Claude Code
points straight at it. **Log events are the primary stream** (authoritative `cost_usd`,
`duration_ms`, all four token classes, `prompt.id`); metrics are accepted and stored but views
prefer events (delta temporality makes raw metrics annoying).

## 1. OTLP endpoints (TDD)

- **Fixtures first:** capture real payloads by running Claude Code once with telemetry pointed at
  a dump script; commit sanitized `tests/fixtures/otlp/*.json` covering, exactly:
  `api_request`, `tool_result`, `tool_decision`, `user_prompt`, `compaction`, `api_error`,
  plus one metrics export.
- RED: tests POSTing fixtures to `/v1/logs` and `/v1/metrics` assert correct ObsEvent rows land
  (cost, tokens, session_id, prompt_id extracted from OTel attributes) and the correct OTLP
  response envelope is returned.
- GREEN: `ingest/otlp.py` — parse OTLP JSON (`resourceLogs` → `scopeLogs` → `logRecords`), map
  attributes → ObsEvent columns, everything else into `payload`.
- RED→GREEN idempotency: replaying the same fixture batch creates no duplicate rows.
  `event_id = hash(source + session_id + timeUnixNano + body)`.
- RED→GREEN robustness: malformed payloads return **400 without crashing the writer**; unknown
  event types are **stored raw, never dropped**.

## 2. End-to-end wiring

- Document (README proposal via backlog + `.env.sample` is lead-owned) the exact Claude Code
  env/settings to emit here; verify a real local session's events appear in DuckDB.

## Testing strategy

FastAPI `TestClient` + canned fixtures — hermetic, no network. Edge cases the spec requires:

- batched multi-record payloads
- missing optional attributes
- **gzip content-encoding** accepted
- **oversized bodies rejected** above a configurable limit
- concurrent posts (writer serialization holds)

## Acceptance

- `uv run pytest tests/unit/ingest/test_otlp.py -q` — all fixture and edge-case tests green.
- `just check` — clean.
- Manual E2E (validator will re-run): `just dev` + one real Claude Code prompt →
  `duckdb $BAM_DB_PATH "SELECT event_type, count(*) FROM events GROUP BY 1"` shows live rows.

## The ONE recorded handoff

You expose a stable `get_router() -> APIRouter` from `ingest/otlp.py`; 🖥 web mounts it in
`web/app.py` under a lead-issued LOAN TICKET on the marked `# otlp-mount` block. You keep
ownership of the router's internals; web keeps ownership of the mount call.
