-- Metric views over the raw `events` table. Applied via store/schema.py::load_views().
-- CREATE OR REPLACE VIEW everywhere so re-applying on every boot is always safe.

-- G6: a source='jsonl' cost row is an ESTIMATE and is excluded whenever an OTel-derived cost
-- exists for the same (session_id, request_id). Every cost-bearing view is built on top of this.
CREATE OR REPLACE VIEW v_cost_events AS
SELECT e.*
FROM events e
WHERE e.cost_usd IS NOT NULL
  AND NOT (
      e.source = 'jsonl'
      AND e.request_id IS NOT NULL
      AND EXISTS (
          SELECT 1
          FROM events o
          WHERE o.source = 'otlp'
            AND o.cost_usd IS NOT NULL
            AND o.request_id = e.request_id
            AND o.session_id IS NOT DISTINCT FROM e.session_id
      )
  );

-- v_sessions: start/end/duration/cost/model per session.
CREATE OR REPLACE VIEW v_sessions AS
WITH session_times AS (
    SELECT
        session_id,
        min(ts) AS started_at,
        max(ts) AS ended_at,
        any_value(model) AS model
    FROM events
    WHERE session_id IS NOT NULL
    GROUP BY session_id
),
session_costs AS (
    SELECT session_id, sum(cost_usd) AS cost_usd
    FROM v_cost_events
    WHERE session_id IS NOT NULL
    GROUP BY session_id
)
SELECT
    st.session_id,
    st.started_at,
    st.ended_at,
    date_diff('millisecond', st.started_at, st.ended_at) AS duration_ms,
    coalesce(sc.cost_usd, 0.0) AS cost_usd,
    st.model
FROM session_times st
LEFT JOIN session_costs sc ON sc.session_id = st.session_id
ORDER BY st.started_at;

-- v_tasks: per prompt_id (one user prompt = one task) — wall-clock, cost, tokens, tool counts.
CREATE OR REPLACE VIEW v_tasks AS
WITH task_times AS (
    SELECT
        prompt_id,
        any_value(session_id) AS session_id,
        min(ts) AS started_at,
        max(ts) AS ended_at,
        sum(tokens_input) AS tokens_input,
        sum(tokens_output) AS tokens_output,
        sum(tokens_cache_read) AS tokens_cache_read,
        sum(tokens_cache_creation) AS tokens_cache_creation,
        count(*) FILTER (WHERE event_type = 'tool_result') AS tool_call_count
    FROM events
    WHERE prompt_id IS NOT NULL
    GROUP BY prompt_id
),
task_costs AS (
    SELECT prompt_id, sum(cost_usd) AS cost_usd
    FROM v_cost_events
    WHERE prompt_id IS NOT NULL
    GROUP BY prompt_id
)
SELECT
    tt.prompt_id,
    tt.session_id,
    tt.started_at,
    tt.ended_at,
    date_diff('millisecond', tt.started_at, tt.ended_at) AS duration_ms,
    coalesce(tc.cost_usd, 0.0) AS cost_usd,
    tt.tokens_input,
    tt.tokens_output,
    tt.tokens_cache_read,
    tt.tokens_cache_creation,
    tt.tool_call_count
FROM task_times tt
LEFT JOIN task_costs tc ON tc.prompt_id = tt.prompt_id
ORDER BY tt.started_at;

-- v_costs_daily: cost + event count per UTC day, G6-excluded.
-- `ts` is a naive TIMESTAMP always written in UTC wall-clock (schema.py::pin_utc) so a plain
-- CAST to DATE is already a UTC day boundary — no per-connection TimeZone dependency.
CREATE OR REPLACE VIEW v_costs_daily AS
SELECT
    CAST(ts AS DATE) AS day,
    sum(cost_usd) AS cost_usd,
    count(*) AS event_count
FROM v_cost_events
GROUP BY 1
ORDER BY 1;

-- v_tool_stats: success rate, p50/p95 duration per tool.
CREATE OR REPLACE VIEW v_tool_stats AS
SELECT
    tool_name,
    count(*) AS call_count,
    count(*) FILTER (WHERE success) AS success_count,
    count(*) FILTER (WHERE success)::DOUBLE / count(*) AS success_rate,
    quantile_cont(duration_ms, 0.5) AS p50_duration_ms,
    quantile_cont(duration_ms, 0.95) AS p95_duration_ms
FROM events
WHERE event_type = 'tool_result' AND tool_name IS NOT NULL
GROUP BY tool_name
ORDER BY tool_name;

-- v_attribution: cost + latency per agent_name/skill_name/model (Anthropic per-feature view).
-- job_run rows (jobs/live.py::persist_job_status) are scheduler bookkeeping with
-- agent_name/skill_name/model all NULL — excluded so they don't inflate the (NULL, NULL, NULL)
-- attribution bucket (BL-08).
CREATE OR REPLACE VIEW v_attribution AS
WITH attributed_stats AS (
    SELECT
        agent_name,
        skill_name,
        model,
        count(*) AS event_count,
        avg(duration_ms) AS avg_duration_ms
    FROM events
    WHERE event_type != 'job_run'
    GROUP BY agent_name, skill_name, model
),
attributed_costs AS (
    SELECT agent_name, skill_name, model, sum(cost_usd) AS cost_usd
    FROM v_cost_events
    GROUP BY agent_name, skill_name, model
)
SELECT
    ast.agent_name,
    ast.skill_name,
    ast.model,
    coalesce(ac.cost_usd, 0.0) AS cost_usd,
    ast.avg_duration_ms,
    ast.event_count
FROM attributed_stats ast
LEFT JOIN attributed_costs ac
    ON ast.agent_name IS NOT DISTINCT FROM ac.agent_name
   AND ast.skill_name IS NOT DISTINCT FROM ac.skill_name
   AND ast.model IS NOT DISTINCT FROM ac.model
ORDER BY ast.agent_name, ast.skill_name, ast.model;

-- v_five_metrics: the "5 metrics that matter" as a single summary row.
CREATE OR REPLACE VIEW v_five_metrics AS
WITH task_errors AS (
    SELECT prompt_id, bool_or(event_type = 'api_error') AS has_error
    FROM events
    WHERE prompt_id IS NOT NULL
    GROUP BY prompt_id
),
completion AS (
    SELECT
        count(*) AS total_tasks,
        count(*) FILTER (WHERE NOT has_error) AS completed_tasks
    FROM task_errors
),
tool_selection AS (
    SELECT
        count(*) AS total_calls,
        count(*) FILTER (WHERE success) AS successful_calls
    FROM events
    WHERE event_type = 'tool_result'
),
autonomy AS (
    SELECT
        count(*) AS total_decisions,
        count(*) FILTER (WHERE json_extract_string(payload, '$.source') = 'config')
            AS config_decisions
    FROM events
    WHERE event_type = 'tool_decision'
),
error_events AS (
    SELECT prompt_id, ts
    FROM events
    WHERE event_type = 'api_error' AND prompt_id IS NOT NULL
),
error_tasks AS (
    SELECT DISTINCT prompt_id FROM error_events
),
recovered_tasks AS (
    SELECT DISTINCT ee.prompt_id
    FROM error_events ee
    WHERE EXISTS (
        SELECT 1
        FROM events later
        WHERE later.prompt_id = ee.prompt_id
          AND later.ts > ee.ts
          AND (
              (later.event_type = 'tool_result' AND later.success)
              OR later.event_type = 'api_request'
          )
    )
),
recovery AS (
    SELECT
        (SELECT count(*) FROM error_tasks) AS total_error_tasks,
        (SELECT count(*) FROM recovered_tasks) AS recovered_tasks
),
successful_task_costs AS (
    -- Denominator scoped to the numerator (specs/outstanding.md P1): a task counts here only
    -- if at least one cost-bearing event (v_cost_events) exists for its prompt_id. The JSONL
    -- backfill carries no cost observation at all; counting those tasks divided real OTel
    -- dollars by the entire transcript history and collapsed the metric toward zero
    -- ($0.00042 on real data). EXISTS on v_cost_events — not `cost_usd > 0` — so a genuinely
    -- $0 costed task still counts, and numerator and denominator are the same population by
    -- construction: denominator = tasks contributing to the numerator.
    SELECT vt.prompt_id, vt.cost_usd
    FROM v_tasks vt
    JOIN task_errors terr ON terr.prompt_id = vt.prompt_id
    WHERE NOT terr.has_error
      AND EXISTS (SELECT 1 FROM v_cost_events ce WHERE ce.prompt_id = vt.prompt_id)
)
SELECT
    CASE WHEN c.total_tasks = 0 THEN NULL
         ELSE c.completed_tasks::DOUBLE / c.total_tasks END AS task_completion_rate,
    CASE WHEN ts.total_calls = 0 THEN NULL
         ELSE ts.successful_calls::DOUBLE / ts.total_calls END AS tool_selection_accuracy,
    CASE WHEN a.total_decisions = 0 THEN NULL
         ELSE a.config_decisions::DOUBLE / a.total_decisions END AS autonomy_score,
    CASE WHEN r.total_error_tasks = 0 THEN NULL
         ELSE r.recovered_tasks::DOUBLE / r.total_error_tasks END AS recovery_rate,
    CASE WHEN (SELECT count(*) FROM successful_task_costs) = 0 THEN NULL
         ELSE (SELECT sum(cost_usd) FROM successful_task_costs)
              / (SELECT count(*) FROM successful_task_costs)
    END AS cost_per_successful_task
FROM completion c, tool_selection ts, autonomy a, recovery r;

-- v_hook_stats: hook executions per (hook_event, hook_name) (outstanding.md P3).
-- Reads hook_execution_complete ONLY — the complete event carries its own totals
-- (num_*, total_duration_ms), so no start/complete pairing is needed;
-- hook_execution_start is deliberately not consumed here.
-- Wire values arrive as JSON *strings* ("456", "3"), hence TRY_CAST.
CREATE OR REPLACE VIEW v_hook_stats AS
SELECT
    json_extract_string(payload, '$.hook_event') AS hook_event,
    json_extract_string(payload, '$.hook_name')  AS hook_name,
    count(*) AS execution_count,
    coalesce(sum(TRY_CAST(json_extract_string(payload, '$.num_success') AS INTEGER)), 0)
        AS hooks_succeeded,
    coalesce(sum(TRY_CAST(json_extract_string(payload, '$.num_blocking') AS INTEGER)
               + TRY_CAST(json_extract_string(payload, '$.num_non_blocking_error') AS INTEGER)), 0)
        AS hooks_errored,
    avg(TRY_CAST(json_extract_string(payload, '$.total_duration_ms') AS DOUBLE))
        AS avg_duration_ms,
    max(ts) AS last_seen
FROM events
WHERE event_type = 'hook_execution_complete'
GROUP BY 1, 2
ORDER BY 1, 2;

-- v_infra_events: hook registrations, plugin loads and MCP connections as one inventory feed
-- (outstanding.md P3). NOTE the dotted payload key: Claude Code emits the literal attribute
-- `plugin.name`, so the JSON path must quote it ('$."plugin.name"') — an unquoted dot is a
-- path separator and the extraction silently NULLs.
-- assistant_response is intentionally NOT here: no captured fixture pins its shape yet, and it
-- is per-response chatter, not infra. hook_execution_* is covered by v_hook_stats.
CREATE OR REPLACE VIEW v_infra_events AS
SELECT
    ts,
    session_id,
    event_type,
    CASE event_type
        WHEN 'plugin_loaded'         THEN json_extract_string(payload, '$."plugin.name"')
        WHEN 'mcp_server_connection' THEN coalesce(
                                              json_extract_string(payload, '$.server_name'),
                                              json_extract_string(payload, '$."plugin.name"'))
        WHEN 'hook_registered'       THEN json_extract_string(payload, '$.hook_event')
    END AS name,
    json_extract_string(payload, '$.status')         AS status,          -- mcp only
    json_extract_string(payload, '$.transport_type') AS transport_type,  -- mcp only
    json_extract_string(payload, '$.hook_source')    AS hook_source,     -- hooks only
    duration_ms                                                          -- promoted for mcp
FROM events
WHERE event_type IN ('hook_registered', 'plugin_loaded', 'mcp_server_connection')
ORDER BY ts DESC;
