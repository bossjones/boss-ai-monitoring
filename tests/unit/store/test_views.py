"""Golden-fixture tests for store/views.sql — canned rows in, exact aggregate rows out.

Never assert against a live moving dataset (store.md #3). Each test seeds a small, fully
hand-computed fixture directly into the `events` table (bypassing the writer, so these are pure
unit tests of the view SQL) and asserts exact output rows.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import duckdb
import pytest

from boss_ai_monitoring.store.schema import EVENT_COLUMNS, ensure_schema, load_views

Event = dict[str, Any]
MakeEvent = Callable[..., Event]


@pytest.fixture
def conn() -> Any:
    connection = duckdb.connect(":memory:")
    ensure_schema(connection)
    load_views(connection)
    yield connection
    connection.close()


def _insert_events(connection: Any, events: list[Event]) -> None:
    columns_sql = ", ".join(EVENT_COLUMNS)
    placeholders_sql = ", ".join(["?"] * len(EVENT_COLUMNS))
    rows = [
        tuple(
            json.dumps(event.get("payload") or {}) if col == "payload" else event.get(col)
            for col in EVENT_COLUMNS
        )
        for event in events
    ]
    connection.executemany(f"INSERT INTO events ({columns_sql}) VALUES ({placeholders_sql})", rows)


T0 = datetime(2026, 1, 1, 0, 0, 0)  # naive UTC — schema.py stores `ts` as TIMESTAMP, not TZ


# ---------------------------------------------------------------------------
# empty DB
# ---------------------------------------------------------------------------


def test_all_views_handle_empty_db(conn: Any) -> None:
    assert conn.execute("SELECT * FROM v_sessions").fetchall() == []
    assert conn.execute("SELECT * FROM v_tasks").fetchall() == []
    assert conn.execute("SELECT * FROM v_costs_daily").fetchall() == []
    assert conn.execute("SELECT * FROM v_tool_stats").fetchall() == []
    assert conn.execute("SELECT * FROM v_attribution").fetchall() == []

    five = conn.execute(
        "SELECT task_completion_rate, tool_selection_accuracy, autonomy_score, "
        "recovery_rate, cost_per_successful_task FROM v_five_metrics"
    ).fetchall()
    assert five == [(None, None, None, None, None)]


# ---------------------------------------------------------------------------
# v_sessions
# ---------------------------------------------------------------------------


def test_v_sessions_aggregates_start_end_duration_cost_model(
    conn: Any, make_event: MakeEvent
) -> None:
    _insert_events(
        conn,
        [
            make_event(
                "s1e1",
                session_id="sess-1",
                ts=T0,
                source="otlp",
                cost_usd=1.0,
                model="claude-x",
            ),
            make_event(
                "s1e2",
                session_id="sess-1",
                ts=T0 + timedelta(minutes=5),
                source="otlp",
                cost_usd=2.0,
                model="claude-x",
            ),
            make_event(
                "s2e1",
                session_id="sess-2",
                ts=T0 + timedelta(days=1),
                source="otlp",
                cost_usd=5.0,
                model="claude-y",
            ),
        ],
    )

    rows = conn.execute(
        "SELECT session_id, started_at, ended_at, duration_ms, cost_usd, model "
        "FROM v_sessions ORDER BY session_id"
    ).fetchall()

    assert rows == [
        ("sess-1", T0, T0 + timedelta(minutes=5), 300_000, 1.0 + 2.0, "claude-x"),
        ("sess-2", T0 + timedelta(days=1), T0 + timedelta(days=1), 0, 5.0, "claude-y"),
    ]


def test_v_sessions_out_of_order_insert_still_computes_min_max(
    conn: Any, make_event: MakeEvent
) -> None:
    later = T0 + timedelta(minutes=10)
    _insert_events(
        conn,
        [
            make_event("late", session_id="sess-1", ts=later, source="otlp", cost_usd=1.0),
            make_event("early", session_id="sess-1", ts=T0, source="otlp", cost_usd=1.0),
        ],
    )

    row = conn.execute(
        "SELECT started_at, ended_at FROM v_sessions WHERE session_id = 'sess-1'"
    ).fetchone()
    assert row == (T0, later)


def test_v_sessions_excludes_jsonl_cost_when_otel_cost_exists_for_same_request(
    conn: Any, make_event: MakeEvent
) -> None:
    _insert_events(
        conn,
        [
            make_event(
                "otel-r1",
                session_id="sess-1",
                ts=T0,
                source="otlp",
                request_id="req-1",
                cost_usd=1.0,
            ),
            make_event(
                "jsonl-r1-dup",
                session_id="sess-1",
                ts=T0 + timedelta(seconds=1),
                source="jsonl",
                request_id="req-1",
                cost_usd=0.9,
            ),
            make_event(
                "jsonl-r2-unmatched",
                session_id="sess-1",
                ts=T0 + timedelta(seconds=2),
                source="jsonl",
                request_id="req-2",
                cost_usd=0.3,
            ),
        ],
    )

    cost = conn.execute("SELECT cost_usd FROM v_sessions WHERE session_id = 'sess-1'").fetchone()[0]
    assert cost == pytest.approx(1.0 + 0.3)


# ---------------------------------------------------------------------------
# v_tasks
# ---------------------------------------------------------------------------


def test_v_tasks_per_prompt_wallclock_cost_tokens_tool_counts(
    conn: Any, make_event: MakeEvent
) -> None:
    _insert_events(
        conn,
        [
            make_event(
                "t1e1",
                prompt_id="task-1",
                session_id="sess-1",
                ts=T0,
                event_type="api_request",
                source="otlp",
                request_id="rq1",
                cost_usd=1.0,
                tokens_input=100,
                tokens_output=50,
                tokens_cache_read=10,
                tokens_cache_creation=5,
            ),
            make_event(
                "t1e2",
                prompt_id="task-1",
                session_id="sess-1",
                ts=T0 + timedelta(seconds=1),
                event_type="tool_result",
                tool_name="Bash",
            ),
            make_event(
                "t1e3",
                prompt_id="task-1",
                session_id="sess-1",
                ts=T0 + timedelta(seconds=2),
                event_type="tool_result",
                tool_name="Read",
            ),
            make_event(
                "t1e4",
                prompt_id="task-1",
                session_id="sess-1",
                ts=T0 + timedelta(seconds=3),
                event_type="user_prompt",
            ),
            make_event(
                "t2e1",
                prompt_id="task-2",
                session_id="sess-1",
                ts=T0 + timedelta(minutes=1),
                event_type="api_request",
                source="otlp",
                request_id="rq2",
                cost_usd=5.0,
                tokens_input=1,
                tokens_output=1,
                tokens_cache_read=0,
                tokens_cache_creation=0,
            ),
        ],
    )

    rows = conn.execute(
        "SELECT prompt_id, started_at, ended_at, duration_ms, cost_usd, tokens_input, "
        "tokens_output, tokens_cache_read, tokens_cache_creation, tool_call_count "
        "FROM v_tasks ORDER BY prompt_id"
    ).fetchall()

    assert rows == [
        ("task-1", T0, T0 + timedelta(seconds=3), 3000, 1.0, 100, 50, 10, 5, 2),
        ("task-2", T0 + timedelta(minutes=1), T0 + timedelta(minutes=1), 0, 5.0, 1, 1, 0, 0, 0),
    ]


def test_v_tasks_ignores_events_with_null_prompt_id(conn: Any, make_event: MakeEvent) -> None:
    _insert_events(
        conn,
        [
            make_event(
                "orphan",
                prompt_id=None,
                session_id="sess-1",
                event_type="tool_result",
                tool_name="Bash",
            ),
        ],
    )

    assert conn.execute("SELECT * FROM v_tasks").fetchall() == []
    # and it must not break v_tool_stats, which doesn't key off prompt_id
    assert conn.execute(
        "SELECT call_count FROM v_tool_stats WHERE tool_name = 'Bash'"
    ).fetchone() == (1,)


# ---------------------------------------------------------------------------
# v_costs_daily
# ---------------------------------------------------------------------------


def test_v_costs_daily_groups_by_day_and_excludes_matched_jsonl(
    conn: Any, make_event: MakeEvent
) -> None:
    day1 = T0
    day2 = T0 + timedelta(days=1)
    _insert_events(
        conn,
        [
            make_event("d1a", ts=day1 + timedelta(hours=10), source="otlp", cost_usd=1.5),
            make_event("d1b", ts=day1 + timedelta(hours=23), source="otlp", cost_usd=2.5),
            make_event(
                "d2a",
                ts=day2 + timedelta(minutes=30),
                source="otlp",
                request_id="rq-day2",
                cost_usd=4.0,
            ),
            make_event(
                "d2a-jsonl-dup",
                ts=day2 + timedelta(hours=1),
                source="jsonl",
                request_id="rq-day2",
                cost_usd=3.9,
            ),
            make_event(
                "d2b-jsonl-solo",
                ts=day2 + timedelta(hours=2),
                source="jsonl",
                request_id="rq-day2b",
                cost_usd=0.2,
            ),
        ],
    )

    rows = conn.execute(
        "SELECT day, cost_usd, event_count FROM v_costs_daily ORDER BY day"
    ).fetchall()
    assert rows == [
        (day1.date(), pytest.approx(4.0), 2),
        (day2.date(), pytest.approx(4.2), 2),
    ]


# ---------------------------------------------------------------------------
# v_tool_stats
# ---------------------------------------------------------------------------


def test_v_tool_stats_success_rate_and_percentiles(conn: Any, make_event: MakeEvent) -> None:
    _insert_events(
        conn,
        [
            make_event(
                "bash1",
                event_type="tool_result",
                tool_name="Bash",
                success=True,
                duration_ms=200,
            ),
            make_event(
                "bash2",
                event_type="tool_result",
                tool_name="Bash",
                success=False,
                duration_ms=300,
            ),
            make_event(
                "bash3",
                event_type="tool_result",
                tool_name="Bash",
                success=True,
                duration_ms=250,
            ),
            make_event(
                "read1",
                event_type="tool_result",
                tool_name="Read",
                success=True,
                duration_ms=150,
            ),
            make_event(
                "read2",
                event_type="tool_result",
                tool_name="Read",
                success=True,
                duration_ms=50,
            ),
        ],
    )

    rows = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT tool_name, call_count, success_count, success_rate, "
            "p50_duration_ms, p95_duration_ms FROM v_tool_stats ORDER BY tool_name"
        ).fetchall()
    }

    bash = rows["Bash"]
    assert bash[0] == 3
    assert bash[1] == 2
    assert bash[2] == pytest.approx(2 / 3)
    assert bash[3] == pytest.approx(250)
    assert bash[4] == pytest.approx(295)

    read = rows["Read"]
    assert read[0] == 2
    assert read[1] == 2
    assert read[2] == pytest.approx(1.0)
    assert read[3] == pytest.approx(100)
    assert read[4] == pytest.approx(145)


# ---------------------------------------------------------------------------
# v_attribution
# ---------------------------------------------------------------------------


def test_v_attribution_cost_and_latency_per_agent_skill_model(
    conn: Any, make_event: MakeEvent
) -> None:
    _insert_events(
        conn,
        [
            make_event(
                "ax1",
                agent_name="agentA",
                skill_name="skillA",
                model="claude-x",
                source="otlp",
                request_id="rqA1",
                cost_usd=1.0,
                duration_ms=100,
            ),
            make_event(
                "ax2-jsonl-dup",
                agent_name="agentA",
                skill_name="skillA",
                model="claude-x",
                source="jsonl",
                request_id="rqA1",
                cost_usd=0.9,
                duration_ms=110,
            ),
            make_event(
                "ax3",
                agent_name="agentA",
                skill_name="skillA",
                model="claude-x",
                source="otlp",
                request_id="rqA2",
                cost_usd=0.5,
                duration_ms=200,
            ),
            make_event(
                "by1",
                agent_name="agentB",
                skill_name=None,
                model="claude-y",
                source="otlp",
                request_id="rqB1",
                cost_usd=2.0,
                duration_ms=300,
            ),
            make_event(
                "nz1",
                agent_name=None,
                skill_name=None,
                model="claude-x",
                event_type="tool_result",
                cost_usd=None,
                duration_ms=50,
            ),
        ],
    )

    rows = {
        (row[0], row[1], row[2]): row[3:]
        for row in conn.execute(
            "SELECT agent_name, skill_name, model, cost_usd, avg_duration_ms, event_count "
            "FROM v_attribution"
        ).fetchall()
    }

    group_x = rows[("agentA", "skillA", "claude-x")]
    assert group_x[0] == pytest.approx(1.5)
    assert group_x[1] == pytest.approx((100 + 110 + 200) / 3)
    assert group_x[2] == 3

    group_y = rows[("agentB", None, "claude-y")]
    assert group_y[0] == pytest.approx(2.0)
    assert group_y[1] == pytest.approx(300)
    assert group_y[2] == 1

    group_null = rows[(None, None, "claude-x")]
    assert group_null[0] == pytest.approx(0.0)
    assert group_null[1] == pytest.approx(50)
    assert group_null[2] == 1


# ---------------------------------------------------------------------------
# v_five_metrics
# ---------------------------------------------------------------------------


def test_v_five_metrics_computes_all_five(conn: Any, make_event: MakeEvent) -> None:
    t0 = T0
    t1 = T0 + timedelta(minutes=1)
    t2 = T0 + timedelta(minutes=2)

    _insert_events(
        conn,
        [
            # task-ok: completes cleanly, one successful + one failed tool_result
            make_event(
                "ok-req",
                prompt_id="p-ok",
                session_id="s5",
                ts=t0,
                event_type="api_request",
                source="otlp",
                request_id="rq-ok",
                cost_usd=3.0,
            ),
            make_event(
                "ok-tool-1",
                prompt_id="p-ok",
                session_id="s5",
                ts=t0 + timedelta(seconds=1),
                event_type="tool_result",
                tool_name="Bash",
                success=True,
            ),
            make_event(
                "ok-tool-2",
                prompt_id="p-ok",
                session_id="s5",
                ts=t0 + timedelta(seconds=2),
                event_type="tool_result",
                tool_name="Read",
                success=False,
            ),
            make_event(
                "ok-decision",
                prompt_id="p-ok",
                session_id="s5",
                ts=t0 + timedelta(seconds=3),
                event_type="tool_decision",
                payload={"source": "config", "extra_field": {"nested": [1, 2, 3]}},
            ),
            # task-rec: errors, then recovers with a later successful tool_result
            make_event(
                "rec-req",
                prompt_id="p-rec",
                session_id="s5",
                ts=t1,
                event_type="api_request",
                source="otlp",
                request_id="rq-rec",
                cost_usd=1.0,
            ),
            make_event(
                "rec-error",
                prompt_id="p-rec",
                session_id="s5",
                ts=t1 + timedelta(seconds=1),
                event_type="api_error",
            ),
            make_event(
                "rec-tool",
                prompt_id="p-rec",
                session_id="s5",
                ts=t1 + timedelta(seconds=2),
                event_type="tool_result",
                tool_name="Bash",
                success=True,
            ),
            make_event(
                "rec-decision",
                prompt_id="p-rec",
                session_id="s5",
                ts=t1 + timedelta(seconds=3),
                event_type="tool_decision",
                payload={"source": "user"},
            ),
            # task-unrec: errors, never recovers
            make_event(
                "unrec-req",
                prompt_id="p-unrec",
                session_id="s5",
                ts=t2,
                event_type="api_request",
                source="otlp",
                request_id="rq-unrec",
                cost_usd=1.0,
            ),
            make_event(
                "unrec-error",
                prompt_id="p-unrec",
                session_id="s5",
                ts=t2 + timedelta(seconds=1),
                event_type="api_error",
            ),
        ],
    )

    row = conn.execute(
        "SELECT task_completion_rate, tool_selection_accuracy, autonomy_score, "
        "recovery_rate, cost_per_successful_task FROM v_five_metrics"
    ).fetchone()

    task_completion_rate, tool_selection_accuracy, autonomy_score, recovery_rate, cost_per_task = (
        row
    )

    assert task_completion_rate == pytest.approx(1 / 3)
    assert tool_selection_accuracy == pytest.approx(2 / 3)
    assert autonomy_score == pytest.approx(0.5)
    assert recovery_rate == pytest.approx(0.5)
    assert cost_per_task == pytest.approx(3.0)
