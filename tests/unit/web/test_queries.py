"""RED-first tests for web/queries.py — hermetic, in-memory DuckDB, no real file on disk.

Exercises the query layer directly against store's real schema+views, seeded by
``tests/unit/web/conftest.py`` (Wave 3: ``fixture_conn`` uses ``store.schema.ensure_schema`` /
``load_views``, so these tests read through the SAME six views production code does).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from boss_ai_monitoring.web import queries

NOW = datetime(2026, 7, 11, 12, 30, 0)


class TestProvenanceFooter:
    def test_empty_db_reports_no_sources_seen(self, fixture_conn):
        footer = queries.get_provenance_footer(fixture_conn, langsmith_configured=True)

        assert {s.source for s in footer.sources} == set(queries.SOURCES)
        assert all(s.last_seen is None and s.event_count == 0 for s in footer.sources)
        assert footer.drift_status == "unknown"

    def test_reports_max_ts_and_count_per_source(self, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1", source="otlp", ts=datetime(2026, 7, 11, 10, 0))
        insert_event(fixture_conn, event_id="e2", source="otlp", ts=datetime(2026, 7, 11, 11, 0))
        insert_event(fixture_conn, event_id="e3", source="jsonl", ts=datetime(2026, 7, 11, 9, 0))

        footer = queries.get_provenance_footer(fixture_conn, langsmith_configured=True)
        by_source = {s.source: s for s in footer.sources}

        assert by_source["otlp"].last_seen == datetime(2026, 7, 11, 11, 0)
        assert by_source["otlp"].event_count == 2
        assert by_source["jsonl"].last_seen == datetime(2026, 7, 11, 9, 0)
        assert by_source["langsmith"].event_count == 0

    def test_reports_latest_status_per_job_from_job_run_events(self, fixture_conn, insert_event):
        """BL-05: jobs' trailing last-run status, read from event_type='job_run' payload rows."""
        insert_event(
            fixture_conn,
            event_id="j1",
            event_type="job_run",
            ts=datetime(2026, 7, 11, 9, 0),
            payload='{"name": "drift_check", "status": "ok"}',
        )
        insert_event(
            fixture_conn,
            event_id="j2",
            event_type="job_run",
            ts=datetime(2026, 7, 11, 11, 0),
            payload='{"name": "drift_check", "status": "error"}',
        )

        footer = queries.get_provenance_footer(fixture_conn, langsmith_configured=True)

        assert len(footer.job_statuses) == 1
        job = footer.job_statuses[0]
        assert job.name == "drift_check"
        assert job.status == "error"  # the later of the two rows wins
        assert job.last_run_at == datetime(2026, 7, 11, 11, 0)

    def test_drift_status_is_unknown_with_no_drift_check_row(self, fixture_conn):
        footer = queries.get_provenance_footer(fixture_conn, langsmith_configured=True)

        assert footer.drift_status == "unknown"

    def test_footer_carries_langsmith_configured_flag(self, fixture_conn):
        """outstanding.md P1(b): the footer must distinguish 'not configured' from 'idle'."""
        configured = queries.get_provenance_footer(fixture_conn, langsmith_configured=True)
        missing = queries.get_provenance_footer(fixture_conn, langsmith_configured=False)

        assert configured.langsmith_configured is True
        assert missing.langsmith_configured is False

    def test_drift_status_is_ok_when_latest_run_clean(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="d1",
            event_type="job_run",
            payload='{"name": "drift_check", "status": "ok", "alert_count": 0}',
        )

        footer = queries.get_provenance_footer(fixture_conn, langsmith_configured=True)

        assert footer.drift_status == "ok"

    def test_drift_status_is_alert_when_latest_run_has_alerts(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="d1",
            event_type="job_run",
            payload='{"name": "drift_check", "status": "ok", "alert_count": 3}',
        )

        footer = queries.get_provenance_footer(fixture_conn, langsmith_configured=True)

        assert footer.drift_status == "alert"

    def test_drift_status_is_error_when_latest_run_errored(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="d1",
            event_type="job_run",
            payload='{"name": "drift_check", "status": "error", "alert_count": 0}',
        )

        footer = queries.get_provenance_footer(fixture_conn, langsmith_configured=True)

        assert footer.drift_status == "error"

    def test_drift_status_uses_only_the_latest_drift_check_run(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="d1",
            event_type="job_run",
            ts=datetime(2026, 7, 11, 9, 0),
            payload='{"name": "drift_check", "status": "error", "alert_count": 5}',
        )
        insert_event(
            fixture_conn,
            event_id="d2",
            event_type="job_run",
            ts=datetime(2026, 7, 11, 11, 0),
            payload='{"name": "drift_check", "status": "ok", "alert_count": 0}',
        )

        footer = queries.get_provenance_footer(fixture_conn, langsmith_configured=True)

        assert footer.drift_status == "ok"


class TestOverview:
    def test_empty_db_returns_friendly_zero_state(self, fixture_conn):
        overview = queries.get_overview(fixture_conn, now=NOW)

        assert overview.today_cost_usd == 0.0
        assert overview.active_sessions == 0
        assert overview.tool_success_rate is None
        assert overview.recent_sessions == []
        assert len(overview.sparkline) == 14

    def test_sums_todays_cost_and_tokens(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="e1",
            ts=datetime(2026, 7, 11, 9, 0),
            cost_usd=0.50,
            tokens_input=100,
            tokens_output=20,
        )
        insert_event(
            fixture_conn,
            event_id="e2",
            ts=datetime(2026, 7, 11, 10, 0),
            cost_usd=0.25,
            tokens_input=50,
            tokens_output=10,
        )
        insert_event(fixture_conn, event_id="e3", ts=datetime(2026, 7, 10, 9, 0), cost_usd=99.0)

        overview = queries.get_overview(fixture_conn, now=NOW)

        assert overview.today_cost_usd == 0.75
        assert overview.tokens_input == 150
        assert overview.tokens_output == 30

    def test_jsonl_cost_excluded_when_matching_otlp_row_exists(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="e-otlp",
            source="otlp",
            session_id="sess-1",
            request_id="req-1",
            cost_usd=1.0,
            ts=NOW,
        )
        insert_event(
            fixture_conn,
            event_id="e-jsonl",
            source="jsonl",
            session_id="sess-1",
            request_id="req-1",
            cost_usd=1.0,
            ts=NOW,
        )

        overview = queries.get_overview(fixture_conn, now=NOW)

        assert overview.today_cost_usd == 1.0, "jsonl estimate must not double the OTel cost"

    def test_jsonl_cost_included_when_no_matching_otlp_row(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="e-jsonl",
            source="jsonl",
            session_id="sess-2",
            request_id="req-2",
            cost_usd=0.42,
            ts=NOW,
        )

        overview = queries.get_overview(fixture_conn, now=NOW)

        assert overview.today_cost_usd == 0.42

    def test_active_sessions_counts_only_recent_activity(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn, event_id="e1", session_id="sess-recent", ts=NOW - timedelta(minutes=1)
        )
        insert_event(
            fixture_conn, event_id="e2", session_id="sess-stale", ts=NOW - timedelta(hours=2)
        )

        overview = queries.get_overview(fixture_conn, now=NOW)

        assert overview.active_sessions == 1

    def test_tool_success_rate_from_tool_result_events(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn, event_id="e1", event_type="tool_result", tool_name="Bash", success=True
        )
        insert_event(
            fixture_conn, event_id="e2", event_type="tool_result", tool_name="Bash", success=True
        )
        insert_event(
            fixture_conn, event_id="e3", event_type="tool_result", tool_name="Bash", success=False
        )

        overview = queries.get_overview(fixture_conn, now=NOW)

        assert overview.tool_success_rate == 2 / 3

    def test_recent_sessions_ordered_most_recent_first(self, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1", session_id="old", ts=datetime(2026, 7, 11, 8, 0))
        insert_event(fixture_conn, event_id="e2", session_id="new", ts=datetime(2026, 7, 11, 11, 0))

        overview = queries.get_overview(fixture_conn, now=NOW)

        assert [s.session_id for s in overview.recent_sessions] == ["new", "old"]


class TestLive:
    def test_empty_db_returns_no_events_or_sessions(self, fixture_conn):
        live = queries.get_live(fixture_conn, now=NOW)

        assert live.recent_events == []
        assert live.active_sessions == []

    def test_recent_events_most_recent_first(self, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1", ts=datetime(2026, 7, 11, 12, 0))
        insert_event(fixture_conn, event_id="e2", ts=datetime(2026, 7, 11, 12, 15))

        live = queries.get_live(fixture_conn, now=NOW)

        assert [e.event_id for e in live.recent_events] == ["e2", "e1"]

    def test_active_session_cards_only_within_window(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="e1",
            session_id="sess-active",
            ts=NOW - timedelta(minutes=2),
            cost_usd=0.10,
        )
        insert_event(
            fixture_conn,
            event_id="e2",
            session_id="sess-idle",
            ts=NOW - timedelta(hours=1),
        )

        live = queries.get_live(fixture_conn, now=NOW)

        assert [c.session_id for c in live.active_sessions] == ["sess-active"]
        assert live.active_sessions[0].running_cost_usd == 0.10


class TestSessionDetail:
    def test_unknown_session_returns_none(self, fixture_conn):
        assert queries.get_session_detail(fixture_conn, "nope") is None

    def test_groups_events_by_prompt_id(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="e1",
            session_id="sess-1",
            prompt_id="p1",
            ts=datetime(2026, 7, 11, 12, 0),
            cost_usd=0.10,
        )
        insert_event(
            fixture_conn,
            event_id="e2",
            session_id="sess-1",
            prompt_id="p1",
            ts=datetime(2026, 7, 11, 12, 1),
            cost_usd=0.05,
        )
        insert_event(
            fixture_conn,
            event_id="e3",
            session_id="sess-1",
            prompt_id="p2",
            ts=datetime(2026, 7, 11, 12, 5),
            cost_usd=0.20,
        )

        detail = queries.get_session_detail(fixture_conn, "sess-1")

        assert detail is not None
        assert [t.prompt_id for t in detail.tasks] == ["p1", "p2"]
        assert detail.tasks[0].cost_usd == pytest.approx(0.15)

    def test_events_with_no_prompt_id_form_their_own_group(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="e1",
            session_id="sess-1",
            prompt_id=None,
            event_type="compaction",
        )

        detail = queries.get_session_detail(fixture_conn, "sess-1")

        assert detail is not None
        assert detail.tasks[0].prompt_id is None

    def test_tool_call_success_and_failure_counts(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="e1",
            session_id="sess-1",
            prompt_id="p1",
            event_type="tool_result",
            success=True,
        )
        insert_event(
            fixture_conn,
            event_id="e2",
            session_id="sess-1",
            prompt_id="p1",
            event_type="tool_result",
            success=False,
        )

        detail = queries.get_session_detail(fixture_conn, "sess-1")

        assert detail is not None
        assert detail.tasks[0].tool_calls_ok == 1
        assert detail.tasks[0].tool_calls_fail == 1


class TestCosts:
    def test_empty_db_returns_empty_rollups(self, fixture_conn):
        costs = queries.get_costs(fixture_conn)

        assert costs.daily == []
        assert costs.weekly == []
        assert costs.attribution == []

    def test_daily_rollup_groups_by_calendar_day(self, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1", ts=datetime(2026, 7, 10, 9, 0), cost_usd=1.0)
        insert_event(fixture_conn, event_id="e2", ts=datetime(2026, 7, 10, 20, 0), cost_usd=2.0)
        insert_event(fixture_conn, event_id="e3", ts=datetime(2026, 7, 11, 9, 0), cost_usd=3.0)

        costs = queries.get_costs(fixture_conn)

        by_day = {d.day: d.cost_usd for d in costs.daily}
        assert by_day[datetime(2026, 7, 10).date()] == 3.0
        assert by_day[datetime(2026, 7, 11).date()] == 3.0

    def test_attribution_groups_by_model_agent_skill_project(self, fixture_conn, insert_event):
        insert_event(
            fixture_conn,
            event_id="e1",
            model="claude-sonnet-5",
            agent_name="web",
            skill_name="tdd",
            cwd="/repo",
            cost_usd=1.0,
        )

        costs = queries.get_costs(fixture_conn)
        dims = {(a.dimension, a.key) for a in costs.attribution}

        assert ("model", "claude-sonnet-5") in dims
        assert ("agent", "web") in dims
        assert ("skill", "tdd") in dims
        assert ("project", "/repo") in dims
