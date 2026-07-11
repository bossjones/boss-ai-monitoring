"""RED-first tests for web/app.py — create_app(settings) -> FastAPI (BL-02 contract).

Wave 1 SHADOW: hermetic. Data comes from `app.dependency_overrides[get_connection]` pointed at an
in-memory DuckDB fixture connection (see conftest.py) — never a live store import, never a real
DuckDB file on disk.
"""

from __future__ import annotations

import pytest

from boss_ai_monitoring.web.app import create_app, get_connection


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


class TestEventsStream:
    def test_stream_endpoint_returns_event_stream_content_type(self, client):
        response = client.get("/api/events/stream")

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
