"""Read-only query layer backing every dashboard panel.

Wave 3: reads through store's six published views (``v_sessions``, ``v_tasks``,
``v_costs_daily``, ``v_tool_stats``, ``v_attribution``, ``v_five_metrics``) plus the helper view
``v_cost_events`` (the G6 jsonl/otlp dedupe, reused rather than reimplemented). A few fields no
view covers (live event feed, per-source freshness, per-prompt agent/skill + tool success/fail
split, the "no prompt_id" task bucket) are read directly off ``events`` -- none of that duplicates
a view's aggregation logic, it only supplements what the views don't expose.

Every function accepts ``conn: duckdb.DuckDBPyConnection | None``. ``None`` means the DuckDB file
doesn't exist yet (no writer has flushed a first batch) -- web never creates it (G5: read-only,
period), so every function degrades to its empty-state shape instead of touching duckdb at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import duckdb

SOURCES: tuple[str, ...] = ("otlp", "jsonl", "langsmith")

_ACTIVE_WINDOW_MINUTES = 5
_RECENT_SESSIONS_LIMIT = 10
_RECENT_EVENTS_LIMIT = 50
_SPARKLINE_DAYS = 14


@dataclass(frozen=True)
class SourceFreshness:
    source: str
    last_seen: datetime | None
    event_count: int


@dataclass(frozen=True)
class JobStatus:
    """BL-05: ⚙️ jobs' trailing-job last-run status, surfaced in the provenance footer.

    Read from ``events`` rows with ``event_type = 'job_run'`` (payload carries name/status) --
    no schema change needed. Empty until jobs' Wave 3 persistence lands; renders as "no data".
    """

    name: str
    status: str
    last_run_at: datetime


@dataclass(frozen=True)
class ProvenanceFooter:
    """Source lineage + freshness + drift badge + jobs' last-run status, on every panel."""

    sources: list[SourceFreshness]
    # ⚙️ jobs owns the drift-check query; "unknown" until that lands (BL-05 sibling item).
    drift_status: str
    job_statuses: list[JobStatus]
    # outstanding.md P1(b): distinguishes "LangSmith not configured" from "configured but idle".
    # Trailing + defaulted so the frozen dataclass stays constructible at every existing site.
    langsmith_configured: bool = True


@dataclass(frozen=True)
class DailyCost:
    day: date
    cost_usd: float


@dataclass(frozen=True)
class WeeklyCost:
    week_start: date
    cost_usd: float


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    started_at: datetime
    ended_at: datetime
    cost_usd: float
    model: str | None


@dataclass(frozen=True)
class McpServer:
    name: str
    status: str


@dataclass(frozen=True)
class InfraSummary:
    """outstanding.md P3: hook/plugin/MCP telemetry, read via v_hook_stats / v_infra_events."""

    hook_executions: int
    hook_errors: int
    plugins_loaded: int
    mcp_servers: list[McpServer]


@dataclass(frozen=True)
class OverviewData:
    today_cost_usd: float
    tokens_input: int
    tokens_output: int
    tokens_cache_read: int
    tokens_cache_creation: int
    active_sessions: int
    tool_success_rate: float | None
    sparkline: list[DailyCost]
    recent_sessions: list[SessionSummary]
    provenance: ProvenanceFooter
    # Trailing + defaulted so existing constructor call sites stay valid (frozen dataclass).
    infra: InfraSummary | None = None


@dataclass(frozen=True)
class EventSummary:
    event_id: str
    ts: datetime
    source: str
    event_type: str
    session_id: str | None
    tool_name: str | None
    cost_usd: float | None


@dataclass(frozen=True)
class ActiveSessionCard:
    session_id: str
    started_at: datetime
    last_event_at: datetime
    running_cost_usd: float
    running_duration_ms: int


@dataclass(frozen=True)
class LiveData:
    recent_events: list[EventSummary]
    active_sessions: list[ActiveSessionCard]
    provenance: ProvenanceFooter


@dataclass(frozen=True)
class TaskRow:
    prompt_id: str | None
    start_ts: datetime
    end_ts: datetime
    duration_ms: int
    cost_usd: float
    tokens_input: int
    tokens_output: int
    tool_calls_ok: int
    tool_calls_fail: int
    agent_name: str | None
    skill_name: str | None
    # TODO(follow-up): best-effort session_id<->thread_id join against LangSmith
    # (shared.md RISK #2).
    langsmith_url: str | None


@dataclass(frozen=True)
class SessionDetail:
    session_id: str
    tasks: list[TaskRow]
    provenance: ProvenanceFooter


@dataclass(frozen=True)
class AttributionRow:
    dimension: str  # "model" | "agent" | "skill" | "project"
    key: str
    cost_usd: float
    event_count: int


@dataclass(frozen=True)
class FiveMetrics:
    """v_five_metrics as a typed row -- "the 5 metrics that matter" (shared.md)."""

    task_completion_rate: float | None
    tool_selection_accuracy: float | None
    autonomy_score: float | None
    recovery_rate: float | None
    cost_per_successful_task: float | None


@dataclass(frozen=True)
class CostsData:
    daily: list[DailyCost]
    weekly: list[WeeklyCost]
    attribution: list[AttributionRow]
    five_metrics: FiveMetrics | None
    provenance: ProvenanceFooter


def get_provenance_footer(
    conn: duckdb.DuckDBPyConnection | None, *, langsmith_configured: bool
) -> ProvenanceFooter:
    # `langsmith_configured` is a REQUIRED keyword on purpose: the footer is the one place the
    # dashboard can explain an absent source, so no caller gets to forget to say (P1(b)).
    if conn is None:
        empty_sources = [SourceFreshness(source=s, last_seen=None, event_count=0) for s in SOURCES]
        return ProvenanceFooter(
            sources=empty_sources,
            drift_status="unknown",
            job_statuses=[],
            langsmith_configured=langsmith_configured,
        )

    rows = conn.execute("SELECT source, MAX(ts), COUNT(*) FROM events GROUP BY source").fetchall()
    by_source = {r[0]: (r[1], r[2]) for r in rows}
    sources = [
        SourceFreshness(
            source=source,
            last_seen=by_source.get(source, (None, 0))[0],
            event_count=by_source.get(source, (None, 0))[1],
        )
        for source in SOURCES
    ]

    job_rows = conn.execute(
        """
        SELECT
            json_extract_string(payload, '$.name') AS job_name,
            json_extract_string(payload, '$.status') AS status,
            ts
        FROM events
        WHERE event_type = 'job_run'
        QUALIFY row_number() OVER (PARTITION BY job_name ORDER BY ts DESC) = 1
        """
    ).fetchall()
    job_statuses = [JobStatus(name=r[0], status=r[1], last_run_at=r[2]) for r in job_rows if r[0]]

    # BL-08: drift_check's latest run carries alert_count (int) alongside status.
    drift_row = conn.execute(
        """
        SELECT
            json_extract_string(payload, '$.status') AS status,
            CAST(json_extract(payload, '$.alert_count') AS INTEGER) AS alert_count
        FROM events
        WHERE event_type = 'job_run' AND json_extract_string(payload, '$.name') = 'drift_check'
        QUALIFY row_number() OVER (ORDER BY ts DESC) = 1
        """
    ).fetchone()
    assert drift_row is not None
    drift_status_value, alert_count = drift_row
    if drift_status_value is None:
        # QUALIFY row_number() = 1 over zero matching rows still yields one (NULL, NULL) row
        # (a duckdb quirk with unpartitioned window functions), not zero rows.
        drift_status = "unknown"
    else:
        if alert_count and alert_count > 0:
            drift_status = "alert"
        elif drift_status_value == "error":
            drift_status = "error"
        else:
            drift_status = "ok"

    return ProvenanceFooter(
        sources=sources,
        drift_status=drift_status,
        job_statuses=job_statuses,
        langsmith_configured=langsmith_configured,
    )


def get_infra_summary(conn: duckdb.DuckDBPyConnection | None) -> InfraSummary:
    """Hook/plugin/MCP telemetry rollup for the overview panel (outstanding.md P3)."""
    if conn is None:
        return InfraSummary(hook_executions=0, hook_errors=0, plugins_loaded=0, mcp_servers=[])

    hook_row = conn.execute(
        "SELECT coalesce(sum(execution_count), 0), coalesce(sum(hooks_errored), 0) "
        "FROM v_hook_stats"
    ).fetchone()
    assert hook_row is not None
    hook_executions, hook_errors = int(hook_row[0]), int(hook_row[1])

    plugin_row = conn.execute(
        "SELECT count(*) FROM v_infra_events WHERE event_type = 'plugin_loaded'"
    ).fetchone()
    assert plugin_row is not None
    plugins_loaded = int(plugin_row[0])

    mcp_rows = conn.execute(
        """
        SELECT name, status
        FROM v_infra_events
        WHERE event_type = 'mcp_server_connection' AND name IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY name ORDER BY ts DESC) = 1
        ORDER BY name
        """
    ).fetchall()
    mcp_servers = [McpServer(name=r[0], status=r[1] or "unknown") for r in mcp_rows]

    return InfraSummary(
        hook_executions=hook_executions,
        hook_errors=hook_errors,
        plugins_loaded=plugins_loaded,
        mcp_servers=mcp_servers,
    )


def get_overview(
    conn: duckdb.DuckDBPyConnection | None,
    *,
    now: datetime,
    langsmith_configured: bool = True,
) -> OverviewData:
    if conn is None:
        return OverviewData(
            today_cost_usd=0.0,
            tokens_input=0,
            tokens_output=0,
            tokens_cache_read=0,
            tokens_cache_creation=0,
            active_sessions=0,
            tool_success_rate=None,
            sparkline=[
                DailyCost(day=now.date() - timedelta(days=i), cost_usd=0.0)
                for i in range(_SPARKLINE_DAYS - 1, -1, -1)
            ],
            recent_sessions=[],
            provenance=get_provenance_footer(conn, langsmith_configured=langsmith_configured),
            infra=get_infra_summary(conn),
        )

    today = now.date()

    cost_row = conn.execute("SELECT cost_usd FROM v_costs_daily WHERE day = ?", [today]).fetchone()
    today_cost = cost_row[0] if cost_row else 0.0

    # Tokens have no dedicated view (v_costs_daily only rolls up cost). G6's jsonl/otlp dedupe is
    # defined for cost_usd specifically -- summing raw tokens for the day is not reimplementing any
    # view's logic, just filling a gap none of the six cover.
    tokens_row = conn.execute(
        """
        SELECT
            COALESCE(SUM(tokens_input), 0),
            COALESCE(SUM(tokens_output), 0),
            COALESCE(SUM(tokens_cache_read), 0),
            COALESCE(SUM(tokens_cache_creation), 0)
        FROM events
        WHERE CAST(ts AS DATE) = ?
        """,
        [today],
    ).fetchone()
    assert tokens_row is not None
    tokens_in, tokens_out, tokens_cache_read, tokens_cache_creation = tokens_row

    active_cutoff = now - timedelta(minutes=_ACTIVE_WINDOW_MINUTES)
    active_row = conn.execute(
        "SELECT COUNT(*) FROM v_sessions WHERE ended_at >= ?", [active_cutoff]
    ).fetchone()
    assert active_row is not None
    active_sessions = active_row[0]

    tool_row = conn.execute(
        "SELECT SUM(call_count), SUM(success_count) FROM v_tool_stats"
    ).fetchone()
    assert tool_row is not None
    total_calls, ok_calls = tool_row
    tool_success_rate = (ok_calls / total_calls) if total_calls else None

    sparkline_start = today - timedelta(days=_SPARKLINE_DAYS - 1)
    sparkline_rows = conn.execute(
        "SELECT day, cost_usd FROM v_costs_daily WHERE day >= ?", [sparkline_start]
    ).fetchall()
    by_day = dict(sparkline_rows)
    sparkline = [
        DailyCost(
            day=sparkline_start + timedelta(days=i),
            cost_usd=by_day.get(sparkline_start + timedelta(days=i), 0.0),
        )
        for i in range(_SPARKLINE_DAYS)
    ]

    session_rows = conn.execute(
        """
        SELECT session_id, started_at, ended_at, cost_usd, model
        FROM v_sessions
        ORDER BY ended_at DESC
        LIMIT ?
        """,
        [_RECENT_SESSIONS_LIMIT],
    ).fetchall()
    recent_sessions = [
        SessionSummary(session_id=r[0], started_at=r[1], ended_at=r[2], cost_usd=r[3], model=r[4])
        for r in session_rows
    ]

    return OverviewData(
        today_cost_usd=today_cost,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_creation=tokens_cache_creation,
        active_sessions=active_sessions,
        tool_success_rate=tool_success_rate,
        sparkline=sparkline,
        recent_sessions=recent_sessions,
        provenance=get_provenance_footer(conn, langsmith_configured=langsmith_configured),
        infra=get_infra_summary(conn),
    )


def get_live(
    conn: duckdb.DuckDBPyConnection | None,
    *,
    now: datetime,
    langsmith_configured: bool = True,
) -> LiveData:
    if conn is None:
        return LiveData(
            recent_events=[],
            active_sessions=[],
            provenance=get_provenance_footer(conn, langsmith_configured=langsmith_configured),
        )

    event_rows = conn.execute(
        """
        SELECT event_id, ts, source, event_type, session_id, tool_name, cost_usd
        FROM events
        ORDER BY ts DESC
        LIMIT ?
        """,
        [_RECENT_EVENTS_LIMIT],
    ).fetchall()
    recent_events = [
        EventSummary(
            event_id=r[0],
            ts=r[1],
            source=r[2],
            event_type=r[3],
            session_id=r[4],
            tool_name=r[5],
            cost_usd=r[6],
        )
        for r in event_rows
    ]

    cutoff = now - timedelta(minutes=_ACTIVE_WINDOW_MINUTES)
    session_rows = conn.execute(
        """
        SELECT session_id, started_at, ended_at, cost_usd, duration_ms
        FROM v_sessions
        WHERE ended_at >= ?
        ORDER BY ended_at DESC
        """,
        [cutoff],
    ).fetchall()
    active_sessions = [
        ActiveSessionCard(
            session_id=r[0],
            started_at=r[1],
            last_event_at=r[2],
            running_cost_usd=r[3],
            running_duration_ms=r[4],
        )
        for r in session_rows
    ]

    return LiveData(
        recent_events=recent_events,
        active_sessions=active_sessions,
        provenance=get_provenance_footer(conn, langsmith_configured=langsmith_configured),
    )


def get_session_detail(
    conn: duckdb.DuckDBPyConnection | None,
    session_id: str,
    *,
    langsmith_configured: bool = True,
) -> SessionDetail | None:
    if conn is None:
        return None

    exists = conn.execute(
        "SELECT 1 FROM events WHERE session_id = ? LIMIT 1", [session_id]
    ).fetchone()
    if exists is None:
        return None

    task_rows = conn.execute(
        """
        SELECT prompt_id, started_at, ended_at, duration_ms, cost_usd, tokens_input, tokens_output
        FROM v_tasks
        WHERE session_id = ?
        ORDER BY started_at
        """,
        [session_id],
    ).fetchall()

    # v_tasks has no agent/skill attribution or tool success/fail split -- supplement per prompt_id
    # from the raw events (not a duplicate of v_tasks' cost/duration/token aggregation).
    enrich_rows = conn.execute(
        """
        SELECT
            prompt_id,
            COUNT(*) FILTER (WHERE event_type = 'tool_result' AND success),
            COUNT(*) FILTER (WHERE event_type = 'tool_result' AND NOT success),
            MAX(agent_name),
            MAX(skill_name)
        FROM events
        WHERE session_id = ? AND prompt_id IS NOT NULL
        GROUP BY prompt_id
        """,
        [session_id],
    ).fetchall()
    enrich = {r[0]: r[1:] for r in enrich_rows}

    tasks = [
        TaskRow(
            prompt_id=r[0],
            start_ts=r[1],
            end_ts=r[2],
            duration_ms=r[3],
            cost_usd=r[4],
            tokens_input=r[5],
            tokens_output=r[6],
            tool_calls_ok=enrich.get(r[0], (0, 0, None, None))[0],
            tool_calls_fail=enrich.get(r[0], (0, 0, None, None))[1],
            agent_name=enrich.get(r[0], (0, 0, None, None))[2],
            skill_name=enrich.get(r[0], (0, 0, None, None))[3],
            langsmith_url=None,
        )
        for r in task_rows
    ]

    # v_tasks filters to prompt_id IS NOT NULL -- sessions with no-prompt_id events (compaction,
    # etc.) need their own bucket read straight off events, per Phase 6's required edge case.
    ungrouped = conn.execute(
        """
        SELECT
            MIN(ts), MAX(ts),
            COALESCE(SUM(cost_usd), 0.0),
            COALESCE(SUM(tokens_input), 0),
            COALESCE(SUM(tokens_output), 0),
            COUNT(*) FILTER (WHERE event_type = 'tool_result' AND success),
            COUNT(*) FILTER (WHERE event_type = 'tool_result' AND NOT success),
            MAX(agent_name),
            MAX(skill_name)
        FROM events
        WHERE session_id = ? AND prompt_id IS NULL
        """,
        [session_id],
    ).fetchone()
    assert ungrouped is not None
    if ungrouped[0] is not None:
        start, end, cost, tin, tout, ok, fail, agent, skill = ungrouped
        tasks.append(
            TaskRow(
                prompt_id=None,
                start_ts=start,
                end_ts=end,
                duration_ms=int((end - start).total_seconds() * 1000),
                cost_usd=cost,
                tokens_input=tin,
                tokens_output=tout,
                tool_calls_ok=ok,
                tool_calls_fail=fail,
                agent_name=agent,
                skill_name=skill,
                langsmith_url=None,
            )
        )
        tasks.sort(key=lambda t: t.start_ts)

    return SessionDetail(
        session_id=session_id,
        tasks=tasks,
        provenance=get_provenance_footer(conn, langsmith_configured=langsmith_configured),
    )


def get_five_metrics(conn: duckdb.DuckDBPyConnection | None) -> FiveMetrics | None:
    if conn is None:
        return None
    row = conn.execute(
        """
        SELECT
            task_completion_rate, tool_selection_accuracy, autonomy_score,
            recovery_rate, cost_per_successful_task
        FROM v_five_metrics
        """
    ).fetchone()
    if row is None:
        return None
    return FiveMetrics(*row)


def get_costs(
    conn: duckdb.DuckDBPyConnection | None, *, langsmith_configured: bool = True
) -> CostsData:
    if conn is None:
        return CostsData(
            daily=[],
            weekly=[],
            attribution=[],
            five_metrics=None,
            provenance=get_provenance_footer(conn, langsmith_configured=langsmith_configured),
        )

    daily_rows = conn.execute("SELECT day, cost_usd FROM v_costs_daily ORDER BY day").fetchall()
    daily = [DailyCost(day=r[0], cost_usd=r[1]) for r in daily_rows]

    weekly_rows = conn.execute(
        """
        SELECT CAST(DATE_TRUNC('week', day) AS DATE) AS week_start, SUM(cost_usd)
        FROM v_costs_daily
        GROUP BY week_start
        ORDER BY week_start
        """
    ).fetchall()
    weekly = [WeeklyCost(week_start=r[0], cost_usd=r[1]) for r in weekly_rows]

    attribution: list[AttributionRow] = []
    for dimension, column in (("model", "model"), ("agent", "agent_name"), ("skill", "skill_name")):
        rows = conn.execute(
            f"""
            SELECT {column}, SUM(cost_usd), SUM(event_count)
            FROM v_attribution
            WHERE {column} IS NOT NULL
            GROUP BY {column}
            ORDER BY 2 DESC
            """
        ).fetchall()
        attribution.extend(
            AttributionRow(dimension=dimension, key=r[0], cost_usd=r[1], event_count=r[2])
            for r in rows
        )

    # v_attribution has no project/cwd dimension -- v_cost_events (store's G6 dedupe helper view,
    # already published in views.sql) supplies it without reimplementing that dedupe ourselves.
    project_rows = conn.execute(
        """
        SELECT cwd, SUM(cost_usd), COUNT(*)
        FROM v_cost_events
        WHERE cwd IS NOT NULL
        GROUP BY cwd
        ORDER BY 2 DESC
        """
    ).fetchall()
    attribution.extend(
        AttributionRow(dimension="project", key=r[0], cost_usd=r[1], event_count=r[2])
        for r in project_rows
    )

    return CostsData(
        daily=daily,
        weekly=weekly,
        attribution=attribution,
        five_metrics=get_five_metrics(conn),
        provenance=get_provenance_footer(conn, langsmith_configured=langsmith_configured),
    )
