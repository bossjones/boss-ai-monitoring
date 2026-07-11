"""Read-only query layer backing every dashboard panel.

Wave 1 SHADOW: queries against the raw ``events`` table directly (the canonical envelope from
``specs/boss-ai-monitoring/briefs/shared.md``, BL-01) rather than store's SQL views, because the
views' exact column shapes are not yet published and web must not block on store's GREEN. The
jsonl/otlp cost dedupe below is a Wave 1 stand-in for the real logic store's ``v_costs_daily`` view
owns (G6) — Wave 3 may replace these bodies with view reads once they land; the dataclass shapes
here are the stable contract templates/app.py render against.

Every function takes a read-only DuckDB connection; nobody here opens one (see web/app.py).
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

# A jsonl row's cost is an estimate; excluded whenever an otlp row exists for the same
# (session_id, request_id) (G6). Reused by every cost aggregation below.
_NON_DUPLICATE_JSONL_CLAUSE = """
    NOT (
        source = 'jsonl'
        AND request_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM events o
            WHERE o.source = 'otlp'
              AND o.session_id = events.session_id
              AND o.request_id = events.request_id
        )
    )
"""


@dataclass(frozen=True)
class SourceFreshness:
    source: str
    last_seen: datetime | None
    event_count: int


@dataclass(frozen=True)
class ProvenanceFooter:
    """Source lineage + freshness + drift badge, rendered on every panel."""

    sources: list[SourceFreshness]
    # Wave 1: always "unknown" -- ⚙️ jobs owns the drift-check query/shape (BL); web wires the
    # rendering once that lands.
    drift_status: str


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
    event_count: int


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
    # TODO(Wave 3): best-effort session_id<->thread_id join against LangSmith (shared.md RISK #2).
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
class CostsData:
    daily: list[DailyCost]
    weekly: list[WeeklyCost]
    attribution: list[AttributionRow]
    provenance: ProvenanceFooter


def get_provenance_footer(conn: duckdb.DuckDBPyConnection) -> ProvenanceFooter:
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
    return ProvenanceFooter(sources=sources, drift_status="unknown")


def get_overview(conn: duckdb.DuckDBPyConnection, *, now: datetime) -> OverviewData:
    today = now.date()

    cost_row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(cost_usd), 0.0),
            COALESCE(SUM(tokens_input), 0),
            COALESCE(SUM(tokens_output), 0),
            COALESCE(SUM(tokens_cache_read), 0),
            COALESCE(SUM(tokens_cache_creation), 0)
        FROM events
        WHERE CAST(ts AS DATE) = ? AND {_NON_DUPLICATE_JSONL_CLAUSE}
        """,
        [today],
    ).fetchone()
    assert cost_row is not None
    today_cost, tokens_in, tokens_out, tokens_cache_read, tokens_cache_creation = cost_row

    active_cutoff = now - timedelta(minutes=_ACTIVE_WINDOW_MINUTES)
    active_row = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM events WHERE session_id IS NOT NULL AND ts >= ?",
        [active_cutoff],
    ).fetchone()
    assert active_row is not None
    active_sessions = active_row[0]

    tool_row = conn.execute(
        """
        SELECT COUNT(*) FILTER (WHERE success), COUNT(*)
        FROM events
        WHERE event_type = 'tool_result'
        """
    ).fetchone()
    assert tool_row is not None
    tool_ok, tool_total = tool_row
    tool_success_rate = (tool_ok / tool_total) if tool_total else None

    sparkline_start = today - timedelta(days=_SPARKLINE_DAYS - 1)
    sparkline_rows = conn.execute(
        f"""
        SELECT CAST(ts AS DATE) AS day, COALESCE(SUM(cost_usd), 0.0)
        FROM events
        WHERE ts >= ? AND {_NON_DUPLICATE_JSONL_CLAUSE}
        GROUP BY day
        """,
        [sparkline_start],
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
        SELECT session_id, MIN(ts), MAX(ts), COALESCE(SUM(cost_usd), 0.0), COUNT(*)
        FROM events
        WHERE session_id IS NOT NULL
        GROUP BY session_id
        ORDER BY MAX(ts) DESC
        LIMIT ?
        """,
        [_RECENT_SESSIONS_LIMIT],
    ).fetchall()
    recent_sessions = [
        SessionSummary(
            session_id=r[0], started_at=r[1], ended_at=r[2], cost_usd=r[3], event_count=r[4]
        )
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
        provenance=get_provenance_footer(conn),
    )


def get_live(conn: duckdb.DuckDBPyConnection, *, now: datetime) -> LiveData:
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
        SELECT session_id, MIN(ts), MAX(ts), COALESCE(SUM(cost_usd), 0.0)
        FROM events
        WHERE session_id IS NOT NULL AND ts >= ?
        GROUP BY session_id
        ORDER BY MAX(ts) DESC
        """,
        [cutoff],
    ).fetchall()
    active_sessions = [
        ActiveSessionCard(
            session_id=r[0],
            started_at=r[1],
            last_event_at=r[2],
            running_cost_usd=r[3],
            running_duration_ms=int((r[2] - r[1]).total_seconds() * 1000),
        )
        for r in session_rows
    ]

    return LiveData(
        recent_events=recent_events,
        active_sessions=active_sessions,
        provenance=get_provenance_footer(conn),
    )


def get_session_detail(conn: duckdb.DuckDBPyConnection, session_id: str) -> SessionDetail | None:
    exists = conn.execute(
        "SELECT 1 FROM events WHERE session_id = ? LIMIT 1", [session_id]
    ).fetchone()
    if exists is None:
        return None

    task_rows = conn.execute(
        """
        SELECT
            prompt_id,
            MIN(ts),
            MAX(ts),
            COALESCE(SUM(cost_usd), 0.0),
            COALESCE(SUM(tokens_input), 0),
            COALESCE(SUM(tokens_output), 0),
            COUNT(*) FILTER (WHERE event_type = 'tool_result' AND success),
            COUNT(*) FILTER (WHERE event_type = 'tool_result' AND NOT success),
            MAX(agent_name),
            MAX(skill_name)
        FROM events
        WHERE session_id = ?
        GROUP BY prompt_id
        ORDER BY MIN(ts)
        """,
        [session_id],
    ).fetchall()
    tasks = [
        TaskRow(
            prompt_id=r[0],
            start_ts=r[1],
            end_ts=r[2],
            duration_ms=int((r[2] - r[1]).total_seconds() * 1000),
            cost_usd=r[3],
            tokens_input=r[4],
            tokens_output=r[5],
            tool_calls_ok=r[6],
            tool_calls_fail=r[7],
            agent_name=r[8],
            skill_name=r[9],
            langsmith_url=None,
        )
        for r in task_rows
    ]

    return SessionDetail(session_id=session_id, tasks=tasks, provenance=get_provenance_footer(conn))


def get_costs(conn: duckdb.DuckDBPyConnection) -> CostsData:
    daily_rows = conn.execute(
        f"""
        SELECT CAST(ts AS DATE) AS day, COALESCE(SUM(cost_usd), 0.0)
        FROM events
        WHERE {_NON_DUPLICATE_JSONL_CLAUSE}
        GROUP BY day
        ORDER BY day
        """
    ).fetchall()
    daily = [DailyCost(day=r[0], cost_usd=r[1]) for r in daily_rows]

    weekly_rows = conn.execute(
        f"""
        SELECT CAST(DATE_TRUNC('week', ts) AS DATE) AS week_start, COALESCE(SUM(cost_usd), 0.0)
        FROM events
        WHERE {_NON_DUPLICATE_JSONL_CLAUSE}
        GROUP BY week_start
        ORDER BY week_start
        """
    ).fetchall()
    weekly = [WeeklyCost(week_start=r[0], cost_usd=r[1]) for r in weekly_rows]

    attribution: list[AttributionRow] = []
    for dimension, column in (
        ("model", "model"),
        ("agent", "agent_name"),
        ("skill", "skill_name"),
        ("project", "cwd"),
    ):
        rows = conn.execute(
            f"""
            SELECT {column}, COALESCE(SUM(cost_usd), 0.0), COUNT(*)
            FROM events
            WHERE {column} IS NOT NULL AND {_NON_DUPLICATE_JSONL_CLAUSE}
            GROUP BY {column}
            ORDER BY 2 DESC
            """
        ).fetchall()
        attribution.extend(
            AttributionRow(dimension=dimension, key=r[0], cost_usd=r[1], event_count=r[2])
            for r in rows
        )

    return CostsData(
        daily=daily, weekly=weekly, attribution=attribution, provenance=get_provenance_footer(conn)
    )
