"""RED-first tests for web/app.py — create_app(settings) -> FastAPI (BL-02 contract).

Wave 3: route/JSON/fragment tests stay hermetic via `app.dependency_overrides[get_connection]`
pointed at an in-memory fixture connection seeded through store's real schema+views
(conftest.py). `_poll_events` (the SSE polling loop) is tested directly against a real tmp DuckDB
file written by `store.writer.EventWriter` -- a live `TestClient` stream never signals disconnect
for an endpoint that polls forever, so route-level streaming is left to the Playwright e2e test
(tests/e2e/test_dashboard.py) which boots a real server.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from boss_ai_monitoring.web.app import _poll_events, create_app, get_connection


@pytest.fixture
def app(settings, fixture_conn):
    application = create_app(settings)
    application.dependency_overrides[get_connection] = lambda: fixture_conn
    return application


@pytest.fixture
def client(app, client_factory):
    return client_factory(app)


class TestContract:
    def test_create_app_returns_a_fastapi_app(self, settings):
        from fastapi import FastAPI

        app = create_app(settings)

        assert isinstance(app, FastAPI)

    def test_otlp_mount_marker_present_for_the_lead_issued_loan_ticket(self):
        import inspect

        from boss_ai_monitoring.web import app as app_module

        source = inspect.getsource(app_module)

        assert "# otlp-mount" in source

    def test_otlp_router_is_mounted(self, settings, client_factory):
        app = create_app(settings)
        client = client_factory(app)

        response = client.post("/v1/logs", json={})

        assert response.status_code == 200, "a 404 here means the otlp-mount region is empty"


class TestMissingDbFile:
    """No writer has flushed a first batch yet -- web must never create the file (G5)."""

    def test_overview_renders_empty_state_without_touching_duckdb(self, settings, client_factory):
        assert not settings.store.db_path.exists()
        app = create_app(settings)
        client = client_factory(app)

        response = client.get("/")

        assert response.status_code == 200
        assert "No sessions yet" in response.text
        assert not settings.store.db_path.exists(), "web must never create the DB file"


class TestOverviewRoute:
    def test_renders_full_page_on_normal_request(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "<html" in response.text
        assert 'id="overview-panel"' in response.text

    def test_renders_fragment_only_on_htmx_request(self, client):
        response = client.get("/", headers={"HX-Request": "true"})

        assert response.status_code == 200
        assert "<html" not in response.text
        assert 'id="overview-panel"' in response.text

    def test_empty_db_shows_friendly_empty_state(self, client):
        response = client.get("/")

        assert "No sessions yet" in response.text

    def test_provenance_footer_present(self, client, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1", source="otlp")

        response = client.get("/")

        assert "provenance-footer" in response.text
        assert "otlp" in response.text

    def test_api_overview_json_twin_matches_page_data(self, client, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1", cost_usd=1.5)

        response = client.get("/api/overview")

        assert response.status_code == 200
        body = response.json()
        assert body["today_cost_usd"] == 1.5
        assert "provenance" in body
        assert {s["source"] for s in body["provenance"]["sources"]} == {
            "otlp",
            "jsonl",
            "langsmith",
        }


class TestLiveRoute:
    def test_renders_full_page(self, client):
        response = client.get("/live")

        assert response.status_code == 200
        assert "<html" in response.text

    def test_renders_fragment_on_htmx_request(self, client):
        response = client.get("/live", headers={"HX-Request": "true"})

        assert "<html" not in response.text

    def test_api_live_json_twin(self, client, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1")

        response = client.get("/api/live")

        assert response.status_code == 200
        body = response.json()
        assert len(body["recent_events"]) == 1
        assert body["recent_events"][0]["event_id"] == "e1"


class TestSessionDetailRoute:
    def test_unknown_session_returns_404(self, client):
        response = client.get("/sessions/nope")

        assert response.status_code == 404

    def test_known_session_renders(self, client, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1", session_id="sess-1", prompt_id="p1")

        response = client.get("/sessions/sess-1")

        assert response.status_code == 200
        assert "sess-1" in response.text

    def test_api_session_json_twin(self, client, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1", session_id="sess-1", prompt_id="p1")

        response = client.get("/api/sessions/sess-1")

        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == "sess-1"
        assert len(body["tasks"]) == 1

    def test_api_unknown_session_returns_404(self, client):
        response = client.get("/api/sessions/nope")

        assert response.status_code == 404


class TestCostsRoute:
    def test_renders_full_page(self, client):
        response = client.get("/costs")

        assert response.status_code == 200
        assert "<html" in response.text

    def test_api_costs_json_twin(self, client, fixture_conn, insert_event):
        insert_event(fixture_conn, event_id="e1", model="claude-sonnet-5", cost_usd=1.0)

        response = client.get("/api/costs")

        assert response.status_code == 200
        body = response.json()
        assert any(row["dimension"] == "model" for row in body["attribution"])

    def test_sub_cent_cost_per_task_never_renders_as_a_false_zero(
        self, client, fixture_conn, insert_event
    ):
        """Caught on the real dashboard: cost_per_successful_task was 0.00042 and the "%.2f"
        format rendered it as "$0.00". A monitoring tool that reports a non-zero cost as zero is
        lying; show "<$0.01" instead.
        """
        for i in range(300):  # many tasks, tiny total cost -> a sub-cent per-task figure
            insert_event(
                fixture_conn,
                event_id=f"tiny-{i}",
                prompt_id=f"p-{i}",
                model="claude-sonnet-5",
                cost_usd=0.0001,
            )

        response = client.get("/costs")

        assert response.status_code == 200
        assert "$0.00<" not in response.text
        assert "&lt;$0.01" in response.text or "<$0.01" in response.text


class _FakeRequest:
    """Drives `_poll_events`'s disconnect check deterministically -- no real ASGI cycle."""

    def __init__(self, disconnect_after_calls: int) -> None:
        self._calls = 0
        self._disconnect_after_calls = disconnect_after_calls

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > self._disconnect_after_calls


class TestPollEvents:
    async def test_yields_nothing_when_db_file_does_not_exist(self, settings):
        request = _FakeRequest(disconnect_after_calls=0)

        events = [item async for item in _poll_events(settings, request)]

        assert events == []

    async def test_yields_new_rows_written_by_the_real_store(self, settings, db_path):
        import json

        from boss_ai_monitoring.store.writer import EventWriter

        with EventWriter(db_path) as writer:
            writer.write(
                {
                    "event_id": "e1",
                    "ts": datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
                    "source": "otlp",
                    "event_type": "api_request",
                    "session_id": "sess-1",
                    "cost_usd": 0.5,
                }
            )

        request = _FakeRequest(disconnect_after_calls=1)

        events = [item async for item in _poll_events(settings, request)]

        assert len(events) == 1
        payload = json.loads(events[0]["data"])
        assert payload["event_id"] == "e1"
        assert payload["source"] == "otlp"

    async def test_does_not_redeliver_rows_already_seen(self, settings, db_path):
        from boss_ai_monitoring.store.writer import EventWriter

        with EventWriter(db_path) as writer:
            writer.write(
                {
                    "event_id": "e1",
                    "ts": datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
                    "source": "otlp",
                    "event_type": "api_request",
                }
            )

        request = _FakeRequest(disconnect_after_calls=2)

        events = [item async for item in _poll_events(settings, request)]

        assert len(events) == 1


class TestLangsmithBadge:
    """outstanding.md P1(b): the dashboard must say WHY LangSmith rows are absent.

    Hermetic w.r.t. ambient direnv exports: the autouse `isolated_env` fixture strips every
    BAM_* var, so "unset" is the default here and "set" is an explicit monkeypatch.setenv.
    """

    def test_footer_shows_badge_when_project_unset(self, client):
        response = client.get("/")

        assert "langsmith: not configured" in response.text
        assert "provenance-footer__langsmith--missing" in response.text
        assert "BAM_INGEST__LANGSMITH_PROJECT" in response.text, (
            "the badge must name the exact env var to set"
        )

    def test_footer_hides_badge_when_project_set(
        self, db_path, fixture_conn, client_factory, monkeypatch
    ):
        from boss_ai_monitoring.config import BamSettings

        monkeypatch.setenv("BAM_STORE__DB_PATH", str(db_path))
        monkeypatch.setenv("BAM_INGEST__LANGSMITH_PROJECT", "proj")
        app = create_app(BamSettings())
        app.dependency_overrides[get_connection] = lambda: fixture_conn
        client = client_factory(app)

        response = client.get("/")

        assert "langsmith: not configured" not in response.text
