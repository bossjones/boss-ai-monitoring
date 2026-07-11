"""OTLP http/json receiver — POST /v1/logs and POST /v1/metrics (Phase 3, otlp brief).

Parses ``resourceLogs -> scopeLogs -> logRecords`` (and the metrics equivalent) into the
canonical event envelope (shared.md / BL-01) and hands the result to the single writer
(``store.writer.get_writer`` — G5: nobody else opens a DuckDB write connection).

G1: http/json only, wherever the app is bound (no gRPC, no otel-collector, ever).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from boss_ai_monitoring.store.schema import EVENT_COLUMNS
from boss_ai_monitoring.store.writer import EventWriter, get_writer

if TYPE_CHECKING:
    from boss_ai_monitoring.config import BamSettings

Event = dict[str, Any]

SOURCE = "otlp"

_FIXED_COLUMNS = frozenset({"event_id", "ts", "source", "event_type", "payload"})
_OPTIONAL_COLUMNS = tuple(column for column in EVENT_COLUMNS if column not in _FIXED_COLUMNS)

# OTel attribute key -> canonical `events` column (shared.md envelope). Anything not listed here
# lands in `payload` untouched -- new attributes/event types need no code changes, let alone a
# migration (otlp.md robustness RED->GREEN).
_ATTR_TO_COLUMN: dict[str, str] = {
    "session.id": "session_id",
    "prompt.id": "prompt_id",
    "request_id": "request_id",
    "model": "model",
    "git_sha": "git_sha",
    "git.sha": "git_sha",
    "agent.name": "agent_name",
    "skill.name": "skill_name",
    "tool_name": "tool_name",
    "cost_usd": "cost_usd",
    "duration_ms": "duration_ms",
    "input_tokens": "tokens_input",
    "output_tokens": "tokens_output",
    "cache_read_tokens": "tokens_cache_read",
    "cache_creation_tokens": "tokens_cache_creation",
    "success": "success",
    "cwd": "cwd",
}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _to_int(value: Any) -> int:
    return int(float(value))


_COLUMN_COERCE: dict[str, Callable[[Any], Any]] = {
    "cost_usd": float,
    "duration_ms": _to_int,
    "tokens_input": _to_int,
    "tokens_output": _to_int,
    "tokens_cache_read": _to_int,
    "tokens_cache_creation": _to_int,
    "success": _to_bool,
}


def _decode_any_value(value: dict[str, Any] | None) -> Any:
    """OTLP JSON ``AnyValue`` -> a plain Python value."""
    if not value:
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        return [_decode_any_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {
            kv["key"]: _decode_any_value(kv.get("value"))
            for kv in value["kvlistValue"].get("values", [])
            if "key" in kv
        }
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def _attrs_to_dict(attrs: list[dict[str, Any]] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kv in attrs or []:
        key = kv.get("key")
        if key is None:
            continue
        result[key] = _decode_any_value(kv.get("value"))
    return result


def _ts_from_unix_nano(value: Any) -> datetime:
    try:
        nanos = int(value)
    except (TypeError, ValueError):
        nanos = 0
    return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)


def _event_id(source: str, session_id: str | None, time_key: str, body: str) -> str:
    """``event_id = hash(source + session_id + timeUnixNano + body)``.

    Deterministic on the record's own content, so replaying the same export batch reproduces the
    same ids and the writer's anti-join on ``event_id`` makes it a no-op (otlp.md idempotency
    RED->GREEN).
    """
    raw = "|".join([source, session_id or "", time_key, body])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _empty_event(event_id: str, ts: datetime, source: str, event_type: str) -> Event:
    event: Event = dict.fromkeys(_OPTIONAL_COLUMNS)
    event.update(
        {"event_id": event_id, "ts": ts, "source": source, "event_type": event_type, "payload": {}}
    )
    return event


def _apply_attrs(event: Event, attrs: dict[str, Any], *, skip: frozenset[str]) -> None:
    """Map known attribute keys onto canonical columns; everything else -> ``payload``."""
    payload: dict[str, Any] = event["payload"]
    for key, value in attrs.items():
        if key in skip or value is None:
            continue
        column = _ATTR_TO_COLUMN.get(key)
        if column is None:
            payload[key] = value
            continue
        coerce = _COLUMN_COERCE.get(column)
        try:
            event[column] = coerce(value) if coerce is not None else value
        except (TypeError, ValueError):
            # A coercion failure is a data-quality problem, not a crash: surface the raw value.
            payload[key] = value


def _build_log_event(resource_attrs: dict[str, Any], record: dict[str, Any]) -> Event:
    record_attrs = _attrs_to_dict(record.get("attributes"))
    merged = {**resource_attrs, **record_attrs}
    event_type = str(merged.get("event.name") or "unknown")
    time_key = str(record.get("timeUnixNano") or record.get("observedTimeUnixNano") or "0")
    ts = _ts_from_unix_nano(time_key)
    body = json.dumps(record, sort_keys=True, default=str)

    event = _empty_event(
        _event_id(SOURCE, merged.get("session.id"), time_key, body), ts, SOURCE, event_type
    )
    _apply_attrs(event, merged, skip=frozenset({"event.name"}))
    return event


def parse_logs_payload(data: dict[str, Any]) -> list[Event]:
    """``resourceLogs -> scopeLogs -> logRecords`` -> canonical events.

    Defensive throughout (``.get(..., [])``): a missing or unexpected substructure yields fewer
    events, never a crash. A genuinely malformed *outer* body (not even a JSON object) is rejected
    with 400 before this is ever called.
    """
    events: list[Event] = []
    for resource_logs in data.get("resourceLogs") or []:
        resource_attrs = _attrs_to_dict((resource_logs.get("resource") or {}).get("attributes"))
        for scope_logs in resource_logs.get("scopeLogs") or []:
            for record in scope_logs.get("logRecords") or []:
                events.append(_build_log_event(resource_attrs, record))
    return events


def _build_metric_events(resource_attrs: dict[str, Any], metric: dict[str, Any]) -> list[Event]:
    name = metric.get("name") or "unknown_metric"
    unit = metric.get("unit")
    events: list[Event] = []
    for kind in ("sum", "gauge", "histogram", "summary"):
        aggregation = metric.get(kind)
        if not aggregation:
            continue
        for point in aggregation.get("dataPoints") or []:
            merged = {**resource_attrs, **_attrs_to_dict(point.get("attributes"))}
            time_key = str(point.get("timeUnixNano") or point.get("startTimeUnixNano") or "0")
            ts = _ts_from_unix_nano(time_key)
            body = json.dumps(point, sort_keys=True, default=str)

            # event_type is the literal "metric" marker from the canonical envelope (BL-01) --
            # the specific OTel metric name is queryable via payload->>'metric_name'.
            event = _empty_event(
                _event_id(SOURCE, merged.get("session.id"), time_key, f"{name}|{body}"),
                ts,
                SOURCE,
                "metric",
            )
            _apply_attrs(event, merged, skip=frozenset())
            event["payload"]["metric_name"] = name
            event["payload"]["unit"] = unit
            value = point.get("asDouble", point.get("asInt"))
            if value is not None:
                event["payload"]["value"] = value
            events.append(event)
    return events


def parse_metrics_payload(data: dict[str, Any]) -> list[Event]:
    """``resourceMetrics -> scopeMetrics -> metrics -> dataPoints`` -> one event row per point.

    Delta temporality makes raw metric math awkward (shared.md), so views prefer the log events
    for cost/duration; these rows exist purely so nothing OTLP sends is ever dropped.
    """
    events: list[Event] = []
    for resource_metrics in data.get("resourceMetrics") or []:
        resource_attrs = _attrs_to_dict((resource_metrics.get("resource") or {}).get("attributes"))
        for scope_metrics in resource_metrics.get("scopeMetrics") or []:
            for metric in scope_metrics.get("metrics") or []:
                events.extend(_build_metric_events(resource_attrs, metric))
    return events


def _reject_if_declared_oversized(request: Request, max_bytes: int) -> None:
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        declared = int(content_length)
    except ValueError:
        return
    if declared > max_bytes:
        raise HTTPException(status_code=413, detail="request body exceeds otlp_max_body_bytes")


async def _read_json_body(request: Request, max_bytes: int) -> Any:
    _reject_if_declared_oversized(request, max_bytes)
    raw = await request.body()

    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise HTTPException(status_code=400, detail="invalid gzip body") from exc

    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="request body exceeds otlp_max_body_bytes")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc}") from exc


# `EventWriter.flush()` only holds its lock around swapping the buffer, not around the
# BEGIN/DELETE/INSERT/COMMIT it then runs (store/writer.py) -- two threads calling write_many()
# then flush() on the same singleton (get_writer) can both reach `BEGIN TRANSACTION` on the one
# shared connection at once and blow up with "cannot start a transaction within a transaction".
# This lock serializes *this router's* writes so concurrent POSTs never trigger that race; it does
# not protect against a concurrent jsonl/langsmith flush on the same writer (see OQ filed against
# store for a proper fix inside EventWriter itself).
_flush_lock = threading.Lock()


def _write_and_flush(writer: EventWriter, events: list[Event]) -> None:
    with _flush_lock:
        writer.write_many(events)
        writer.flush()


def get_router() -> APIRouter:
    """The stable handoff surface (otlp.md "The ONE recorded handoff"): 🖥 web mounts this inside
    ``web/app.py`` under the ``# otlp-mount`` marker, under a lead-issued loan ticket. Router
    internals stay owned here; web only owns the mount call. Neither pane wires ports.
    """
    router = APIRouter(tags=["otlp"])

    @router.post("/v1/logs")
    async def receive_logs(request: Request) -> JSONResponse:
        settings: BamSettings = request.app.state.settings
        data = await _read_json_body(request, settings.ingest.otlp_max_body_bytes)
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="ExportLogsServiceRequest must be a JSON object"
            )

        _write_and_flush(get_writer(settings), parse_logs_payload(data))
        return JSONResponse({})

    @router.post("/v1/metrics")
    async def receive_metrics(request: Request) -> JSONResponse:
        settings: BamSettings = request.app.state.settings
        data = await _read_json_body(request, settings.ingest.otlp_max_body_bytes)
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="ExportMetricsServiceRequest must be a JSON object"
            )

        _write_and_flush(get_writer(settings), parse_metrics_payload(data))
        return JSONResponse({})

    return router
