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


class MalformedPayloadError(ValueError):
    """Body was valid JSON but not a valid OTLP structure. The routes turn this into a 400.

    Without it, an exporter sending JSON of the wrong SHAPE (`{"resourceLogs": ["x"]}`, or an
    `intValue` of `"abc"`) reached `.get()` on a `str` / a bare `int()` and 500'd. Bad *JSON* was
    already rejected with 400; bad *shape* crashed the receiver.
    """


def _as_mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MalformedPayloadError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value


def _decode_any_value(value: dict[str, Any] | None) -> Any:
    """OTLP JSON ``AnyValue`` -> a plain Python value.

    Every cast is guarded: these fields come straight off the wire from an arbitrary exporter.
    """
    if not value:
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        try:
            return int(value["intValue"])
        except (TypeError, ValueError) as exc:
            raise MalformedPayloadError(
                f"intValue is not an integer: {value['intValue']!r}"
            ) from exc
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError) as exc:
            raise MalformedPayloadError(
                f"doubleValue is not a number: {value['doubleValue']!r}"
            ) from exc
    if "arrayValue" in value:
        values = _as_mapping(value["arrayValue"], "arrayValue").get("values") or []
        return [_decode_any_value(_as_mapping(v, "arrayValue entry")) for v in values]
    if "kvlistValue" in value:
        entries = _as_mapping(value["kvlistValue"], "kvlistValue").get("values") or []
        return {
            kv["key"]: _decode_any_value(kv.get("value"))
            for kv in (_as_mapping(e, "kvlistValue entry") for e in entries)
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

    A *missing* substructure yields fewer events, never a crash. A substructure of the wrong TYPE
    raises `MalformedPayloadError`, which the route turns into a 400 — the `.get(..., [])` guards
    only ever covered the lists, so a non-mapping element used to reach `.get()` on a `str` and
    500 the receiver.
    """
    events: list[Event] = []
    for resource_logs in data.get("resourceLogs") or []:
        resource_logs = _as_mapping(resource_logs, "resourceLogs entry")
        resource = _as_mapping(resource_logs.get("resource") or {}, "resource")
        resource_attrs = _attrs_to_dict(resource.get("attributes"))
        for scope_logs in resource_logs.get("scopeLogs") or []:
            scope_logs = _as_mapping(scope_logs, "scopeLogs entry")
            for record in scope_logs.get("logRecords") or []:
                events.append(_build_log_event(resource_attrs, _as_mapping(record, "logRecord")))
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
        resource_metrics = _as_mapping(resource_metrics, "resourceMetrics entry")
        resource = _as_mapping(resource_metrics.get("resource") or {}, "resource")
        resource_attrs = _attrs_to_dict(resource.get("attributes"))
        for scope_metrics in resource_metrics.get("scopeMetrics") or []:
            scope_metrics = _as_mapping(scope_metrics, "scopeMetrics entry")
            for metric in scope_metrics.get("metrics") or []:
                events.extend(_build_metric_events(resource_attrs, _as_mapping(metric, "metric")))
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


def _write_and_flush(writer: EventWriter, events: list[Event]) -> None:
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

        try:
            events = parse_logs_payload(data)
        except MalformedPayloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        _write_and_flush(get_writer(settings), events)
        return JSONResponse({})

    @router.post("/v1/metrics")
    async def receive_metrics(request: Request) -> JSONResponse:
        settings: BamSettings = request.app.state.settings
        data = await _read_json_body(request, settings.ingest.otlp_max_body_bytes)
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=400, detail="ExportMetricsServiceRequest must be a JSON object"
            )

        try:
            events = parse_metrics_payload(data)
        except MalformedPayloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        _write_and_flush(get_writer(settings), events)
        return JSONResponse({})

    return router
