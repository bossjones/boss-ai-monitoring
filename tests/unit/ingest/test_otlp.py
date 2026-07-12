"""RED-first tests for ingest/otlp.py — OTLP http/json receiver (Phase 3, otlp brief).

Hermetic: FastAPI TestClient + canned fixtures under tests/fixtures/otlp/, no network. The router
is mounted into a bare FastAPI() app with `app.state.settings` set directly -- exactly how
`web/app.py` will do it under the lead-issued loan ticket, without depending on `create_app`.
"""

from __future__ import annotations

import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import duckdb
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from boss_ai_monitoring.ingest.otlp import get_router

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "otlp"

LOG_FIXTURES = [
    "api_request.json",
    "tool_result.json",
    "tool_decision.json",
    "user_prompt.json",
    "compaction.json",
    "api_error.json",
]


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text())


def _events(db_path: Path) -> list[dict[str, Any]]:
    """Peek at rows via a second same-config (non-read-only) connection.

    The router's writer is a process-wide singleton (BL-01 `get_writer`) that stays open for the
    life of the test process, so `connect_read_only` would collide on mismatched config -- same
    workaround store's own `tests/unit/store/test_writer.py::_peek_count` uses.
    """
    conn = duckdb.connect(str(db_path))
    try:
        columns = [d[0] for d in conn.execute("SELECT * FROM events").description]
        rows = conn.execute("SELECT * FROM events ORDER BY ts").fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]
    finally:
        conn.close()


@pytest.fixture
def app(settings: Any) -> FastAPI:
    application = FastAPI()
    application.state.settings = settings
    application.include_router(get_router())
    return application


@pytest.fixture
def client(app: FastAPI, client_factory: Any) -> TestClient:
    return client_factory(app)


class TestRouterContract:
    def test_get_router_returns_an_api_router(self) -> None:
        assert isinstance(get_router(), APIRouter)


class TestLogsEndpoint:
    @pytest.mark.parametrize("fixture_name", LOG_FIXTURES)
    def test_accepts_each_named_event_fixture_with_otlp_envelope(
        self, client: TestClient, fixture_name: str
    ) -> None:
        response = client.post("/v1/logs", json=_load_fixture(fixture_name))

        assert response.status_code == 200
        assert response.json() == {}

    def test_api_request_fixture_lands_authoritative_columns(
        self, client: TestClient, db_path: Path
    ) -> None:
        client.post("/v1/logs", json=_load_fixture("api_request.json"))

        rows = _events(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["source"] == "otlp"
        assert row["event_type"] == "api_request"
        assert row["session_id"] == "5b1e3b7a-9e3a-4f7b-9c0e-2e6b6b2e6b6b"
        assert row["prompt_id"] == "prompt-7f3c1a2b"
        assert row["request_id"] == "req_01H8X2ZQK3M4N5P6Q7R8S9T0U1"
        assert row["model"] == "claude-sonnet-5"
        assert row["cost_usd"] == pytest.approx(0.0421)
        assert row["duration_ms"] == 3542
        assert row["tokens_input"] == 1820
        assert row["tokens_output"] == 412
        assert row["tokens_cache_read"] == 900
        assert row["tokens_cache_creation"] == 0

    def test_tool_result_fixture_lands_success_and_tool_name(
        self, client: TestClient, db_path: Path
    ) -> None:
        client.post("/v1/logs", json=_load_fixture("tool_result.json"))

        row = _events(db_path)[0]
        assert row["event_type"] == "tool_result"
        assert row["tool_name"] == "Bash"
        assert row["success"] is True
        assert row["duration_ms"] == 812

    def test_tool_decision_fixture_lands_decision_and_source_in_payload(
        self, client: TestClient, db_path: Path
    ) -> None:
        client.post("/v1/logs", json=_load_fixture("tool_decision.json"))

        row = _events(db_path)[0]
        assert row["event_type"] == "tool_decision"
        payload = json.loads(row["payload"])
        assert payload["decision"] == "accept"
        assert payload["source"] == "config"

    def test_api_error_fixture_lands_request_id_and_error_details(
        self, client: TestClient, db_path: Path
    ) -> None:
        client.post("/v1/logs", json=_load_fixture("api_error.json"))

        row = _events(db_path)[0]
        assert row["event_type"] == "api_error"
        assert row["request_id"] == "req_01H8X2ZQK3M4N5P6Q7R8S9T0U2"
        payload = json.loads(row["payload"])
        assert payload["error"] == "overloaded_error"
        assert payload["status_code"] == 529

    def test_compaction_fixture_lands_success_bool_and_token_counts_in_payload(
        self, client: TestClient, db_path: Path
    ) -> None:
        client.post("/v1/logs", json=_load_fixture("compaction.json"))

        row = _events(db_path)[0]
        assert row["event_type"] == "compaction"
        assert row["success"] is True
        payload = json.loads(row["payload"])
        assert payload["pre_tokens"] == 182000
        assert payload["post_tokens"] == 41000

    def test_missing_optional_attributes_do_not_crash(
        self, client: TestClient, db_path: Path
    ) -> None:
        minimal = {
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [
                        {
                            "scope": {"name": "com.anthropic.claude_code"},
                            "logRecords": [
                                {
                                    "timeUnixNano": "1783764200000000000",
                                    "attributes": [
                                        {
                                            "key": "event.name",
                                            "value": {"stringValue": "user_prompt"},
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        response = client.post("/v1/logs", json=minimal)

        assert response.status_code == 200
        row = _events(db_path)[0]
        assert row["session_id"] is None
        assert row["prompt_id"] is None
        assert row["request_id"] is None

    def test_unknown_event_type_stored_raw_not_dropped(
        self, client: TestClient, db_path: Path
    ) -> None:
        unknown = {
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [
                        {
                            "scope": {"name": "com.anthropic.claude_code"},
                            "logRecords": [
                                {
                                    "timeUnixNano": "1783764300000000000",
                                    "attributes": [
                                        {
                                            "key": "event.name",
                                            "value": {"stringValue": "some_future_event"},
                                        },
                                        {
                                            "key": "novel_attr",
                                            "value": {"stringValue": "novel"},
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        response = client.post("/v1/logs", json=unknown)

        assert response.status_code == 200
        row = _events(db_path)[0]
        assert row["event_type"] == "some_future_event"
        payload = json.loads(row["payload"])
        assert payload["novel_attr"] == "novel"

    def test_batched_multi_record_payload_writes_every_record(
        self, client: TestClient, db_path: Path
    ) -> None:
        first = _load_fixture("api_request.json")
        second = _load_fixture("tool_result.json")
        combined = {"resourceLogs": [first["resourceLogs"][0], second["resourceLogs"][0]]}

        client.post("/v1/logs", json=combined)

        rows = _events(db_path)
        assert len(rows) == 2
        assert {row["event_type"] for row in rows} == {"api_request", "tool_result"}

    def test_replaying_the_same_batch_creates_no_duplicate_rows(
        self, client: TestClient, db_path: Path
    ) -> None:
        payload = _load_fixture("api_request.json")

        client.post("/v1/logs", json=payload)
        client.post("/v1/logs", json=payload)

        assert len(_events(db_path)) == 1

    def test_malformed_json_returns_400_without_crashing_the_writer(
        self, client: TestClient, db_path: Path
    ) -> None:
        response = client.post(
            "/v1/logs",
            content=b"{not-valid-json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400

        follow_up = client.post("/v1/logs", json=_load_fixture("api_request.json"))
        assert follow_up.status_code == 200
        assert len(_events(db_path)) == 1

    def test_malformed_top_level_shape_returns_400(self, client: TestClient) -> None:
        response = client.post("/v1/logs", json=["not", "a", "mapping"])

        assert response.status_code == 400

    def test_gzip_content_encoding_accepted(self, client: TestClient, db_path: Path) -> None:
        payload = _load_fixture("api_request.json")
        compressed = gzip.compress(json.dumps(payload).encode("utf-8"))

        response = client.post(
            "/v1/logs",
            content=compressed,
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

        assert response.status_code == 200
        assert len(_events(db_path)) == 1

    def test_oversized_body_rejected_above_configured_limit(
        self, settings: Any, db_path: Path
    ) -> None:
        settings.ingest.otlp_max_body_bytes = 64
        application = FastAPI()
        application.state.settings = settings
        application.include_router(get_router())

        with TestClient(application) as oversized_client:
            response = oversized_client.post("/v1/logs", json=_load_fixture("api_request.json"))

        assert response.status_code == 413
        assert not db_path.exists()

    def test_concurrent_posts_are_serialized_without_data_loss(
        self, client: TestClient, db_path: Path
    ) -> None:
        fixtures = [_load_fixture(name) for name in LOG_FIXTURES]

        def _post(payload: dict[str, Any]) -> int:
            return client.post("/v1/logs", json=payload).status_code

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(_post, fixtures))

        assert all(status == 200 for status in statuses)
        assert len(_events(db_path)) == len(LOG_FIXTURES)


class TestMetricsEndpoint:
    def test_accepts_metrics_export_with_otlp_envelope(self, client: TestClient) -> None:
        response = client.post("/v1/metrics", json=_load_fixture("metrics_export.json"))

        assert response.status_code == 200
        assert response.json() == {}

    def test_metrics_export_lands_one_row_per_data_point(
        self, client: TestClient, db_path: Path
    ) -> None:
        client.post("/v1/metrics", json=_load_fixture("metrics_export.json"))

        rows = _events(db_path)
        assert len(rows) == 2
        assert all(row["event_type"] == "metric" for row in rows)
        payloads = [json.loads(row["payload"]) for row in rows]
        assert {p["metric_name"] for p in payloads} == {"claude_code.token.usage"}
        assert {p["type"] for p in payloads} == {"input", "output"}
        assert all(row["session_id"] == "5b1e3b7a-9e3a-4f7b-9c0e-2e6b6b2e6b6b" for row in rows)

    def test_malformed_metrics_json_returns_400(self, client: TestClient) -> None:
        response = client.post(
            "/v1/metrics", content=b"not json", headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 400


class TestRealCapturedSession:
    """OQ-03: the other fixtures were built from Anthropic's published docs, not a live session.

    This one is a REAL OTLP export, captured at the wire from `claude -p` with telemetry on
    (2026-07-11, Claude Code 2.1.207), then sanitized — user.email / user.id / account uuids /
    organization.id / session.id replaced with placeholders. Nothing else was touched.

    Capturing it found something the doc-derived fixtures missed: a real session emits event types
    we never modeled (hook_registered, plugin_loaded, mcp_server_connection, hook_execution_*).
    They must land raw in `payload` rather than crash or be dropped — that is the whole point of
    the JSON payload column (G5), and until now it was only tested against an invented
    "some_future_event".
    """

    REAL = Path(__file__).resolve().parents[2] / "fixtures" / "otlp" / "real_session_logs.json"

    def test_real_export_is_accepted_and_nothing_is_dropped(
        self, client: TestClient, db_path: Path
    ) -> None:
        payload = json.loads(self.REAL.read_text())
        sent = [
            attr["value"]["stringValue"]
            for rl in payload["resourceLogs"]
            for sl in rl["scopeLogs"]
            for rec in sl["logRecords"]
            for attr in rec.get("attributes", [])
            if attr["key"] == "event.name"
        ]

        response = client.post("/v1/logs", json=payload)

        assert response.status_code == 200
        stored = {row["event_type"] for row in _events(db_path)}
        assert stored == set(sent), "every real event type must be stored, not silently dropped"

    def test_event_types_absent_from_our_schema_are_kept_raw(
        self, client: TestClient, db_path: Path
    ) -> None:
        """The types a live session actually emits, which no doc-derived fixture covered."""
        payload = json.loads(self.REAL.read_text())

        assert client.post("/v1/logs", json=payload).status_code == 200

        rows = {row["event_type"]: row for row in _events(db_path)}
        for unmodelled in ("hook_registered", "plugin_loaded", "mcp_server_connection"):
            assert unmodelled in rows, f"{unmodelled} is emitted by real sessions and was dropped"
            assert json.loads(rows[unmodelled]["payload"]), f"{unmodelled} payload must be kept raw"

    def test_prompt_content_is_redacted_by_claude_code_itself(self) -> None:
        """G8, verified against the wire: with the content-capture flags OFF (the default),
        Claude Code sends `prompt=<REDACTED>` and only the LENGTH survives. We never had to
        redact it ourselves — but if a future version stops doing so, this test says so loudly.
        """
        payload = json.loads(self.REAL.read_text())
        attrs = {
            attr["key"]: attr["value"].get("stringValue")
            for rl in payload["resourceLogs"]
            for sl in rl["scopeLogs"]
            for rec in sl["logRecords"]
            for attr in rec.get("attributes", [])
        }

        assert attrs.get("prompt") == "<REDACTED>"
        assert attrs.get("prompt_length") == "30"
