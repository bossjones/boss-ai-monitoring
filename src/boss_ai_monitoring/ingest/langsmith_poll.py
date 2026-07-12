"""LangSmith run poller (Phase 5): pulls back down what the langsmith-claude-code-plugins hook
plugin already pushed up. LangSmith is enrichment, not a dependency — a missing or invalid API
key disables the poller with a visible status, never a crash (G14, shared.md).

Auth is ambient (direnv-injected `LANGSMITH_API_KEY`) — this module never reads it itself; it
inspects the SDK client's own resolved ``.api_key`` property instead, so no ``os.environ`` call
lives outside config.py. The ``thread_id`` <-> ``session_id`` join is best-effort (spec RISK #2):
a run only gets ``session_id`` set when its LangSmith ``thread_id`` metadata exactly matches a
session_id already present locally; anything else lands with ``session_id = NULL`` — a visible
"LangSmith-only" bucket, never silently merged. See OQ-jsonl-02.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from langsmith import AsyncClient
from langsmith import utils as ls_utils

from boss_ai_monitoring.store.schema import EVENT_COLUMNS

if TYPE_CHECKING:
    from langsmith import schemas as ls_schemas

    from boss_ai_monitoring.store.writer import EventWriter

logger = logging.getLogger(__name__)

Event = dict[str, Any]

SOURCE = "langsmith"

# The first poll for a project has no cursor yet. Unlike the JSONL scanner (byte offset 0 is
# cheap), backfilling "since forever" here is not: the API's ~10 req/10s rate limit is scoped to
# <=7-day windows (shared.md), and an unbounded start_time was observed in manual E2E to trigger
# sustained 429s against a real project with meaningful history. So the first poll only reaches
# back 7 days — deep history backfill is a separate concern, not this poller's job.
_DEFAULT_LOOKBACK = timedelta(days=7)

# Re-request the last minute of the previous window on every poll so a run whose start_time
# arrives slightly out of order (clock skew between the LangSmith backend and this host) is never
# missed. Safe because the writer dedupes on event_id (derived from run.id) — reprocessing the
# overlap is a no-op, not a duplicate row.
_CURSOR_OVERLAP = timedelta(seconds=60)

_SELECT_FIELDS = (
    "id",
    "trace_id",
    "run_type",
    "name",
    "start_time",
    "end_time",
    "status",
    "error",
    "extra",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "total_cost",
    "feedback_stats",
)


def _event_defaults() -> Event:
    return dict.fromkeys(EVENT_COLUMNS)


def _known_session_ids(writer: EventWriter) -> set[str]:
    """Local session_ids to join LangSmith `thread_id` against.

    DuckDB refuses a second `read_only` connection to a file that already has a non-read-only
    one open (the writer's), so this reads through the writer's own connection rather than
    opening a fresh one — no new connection, the single-writer invariant (G5) still holds.
    """
    rows = writer._conn.execute(
        "SELECT DISTINCT session_id FROM events WHERE session_id IS NOT NULL"
    ).fetchall()
    return {row[0] for row in rows}


def _run_to_event(run: ls_schemas.Run, *, session_id: str | None) -> Event:
    event = _event_defaults()
    latency_s = run.latency
    success: bool | None = None
    if run.status:
        success = run.status == "success"
    elif run.error is not None:
        success = False
    event.update(
        event_id=f"{SOURCE}:{run.id}",
        ts=run.start_time,
        source=SOURCE,
        event_type="langsmith_run",
        session_id=session_id,
        model=run.metadata.get("ls_model_name"),
        agent_name=run.metadata.get("agent_name"),
        skill_name=run.metadata.get("skill_name"),
        tool_name=run.name if run.run_type == "tool" else None,
        cost_usd=float(run.total_cost) if run.total_cost is not None else None,
        duration_ms=round(latency_s * 1000) if latency_s is not None else None,
        tokens_input=run.prompt_tokens,
        tokens_output=run.completion_tokens,
        success=success,
        payload=run.model_dump(mode="json"),
    )
    return event


@dataclass
class PollResult:
    status: Literal["ok", "disabled", "error"]
    runs_seen: int = 0
    events_written: int = 0
    unmatched_runs: int = 0
    detail: str | None = None


async def poll_once(
    project_name: str,
    writer: EventWriter,
    *,
    client: AsyncClient | None = None,
    cursor_key: str | None = None,
    max_retries: int = 5,
    limit: int = 100,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PollResult:
    """One poll pass: list runs since the persisted cursor, map to events, advance the cursor.

    Never raises for expected failure modes (missing/invalid key, sustained rate limiting) — those
    come back as a ``PollResult`` with ``status`` set accordingly so the caller can surface it.

    ``limit`` caps runs fetched in one pass: the SDK paginates internally, and on a project with
    heavy concurrent trace volume, unpaginated listing was observed in manual E2E to exhaust the
    ~10 req/10s budget within a single poll. A bounded pass costs nothing — runs are fetched
    oldest-first (``order="asc"``; the API defaults to newest-first, which would let a capped
    pass advance the cursor past the unfetched older backlog and lose it), so whatever's left is
    picked up by the next poll interval via the persisted cursor. The API caps ``limit`` at 100
    (``/runs/query`` returns 400 above that, and the async SDK sends it verbatim as the page
    size), so values above 100 are clamped.
    """
    owns_client = client is None
    client = client or AsyncClient()
    try:
        if not client.api_key:
            return PollResult(status="disabled", detail="LANGSMITH_API_KEY not set")

        key = cursor_key or project_name
        cursor_raw = writer.get_cursor(SOURCE, key)
        start_time = (
            datetime.fromisoformat(cursor_raw)
            if cursor_raw
            else datetime.now(UTC) - _DEFAULT_LOOKBACK
        )

        runs: list[ls_schemas.Run] = []
        attempt = 0
        while True:
            try:
                runs = []
                async for run in client.list_runs(
                    project_name=project_name,
                    start_time=start_time,
                    select=list(_SELECT_FIELDS),
                    # /runs/query rejects a body limit > 100, and AsyncClient.list_runs passes
                    # this straight through as the page size — clamp so no caller can 400 us.
                    limit=min(limit, 100),
                    # The API defaults to newest-first, under which a pass that fills the cap
                    # advances the cursor past the UNFETCHED older backlog and drops it forever.
                    # Oldest-first makes the cursor floor the remainder so the next pass drains it.
                    order="asc",
                ):
                    runs.append(run)
                break
            except ls_utils.LangSmithRateLimitError as exc:
                attempt += 1
                if attempt > max_retries:
                    logger.warning("langsmith: giving up after %d rate-limit retries", attempt)
                    return PollResult(status="error", detail=f"rate limited: {exc}")
                await sleep(2**attempt)
            except ls_utils.LangSmithAuthError as exc:
                return PollResult(status="disabled", detail=f"auth failed: {exc}")

        known_sessions = _known_session_ids(writer) if runs else set()

        events: list[Event] = []
        unmatched_runs = 0
        latest_start: datetime | None = None
        for run in runs:
            thread_id = run.metadata.get("thread_id")
            matched = thread_id is not None and thread_id in known_sessions
            if thread_id is not None and not matched:
                unmatched_runs += 1
            events.append(_run_to_event(run, session_id=thread_id if matched else None))
            if latest_start is None or run.start_time > latest_start:
                latest_start = run.start_time

        if events:
            writer.write_many(events)
        if latest_start is not None:
            writer.set_cursor(SOURCE, key, (latest_start - _CURSOR_OVERLAP).isoformat())

        return PollResult(
            status="ok",
            runs_seen=len(runs),
            events_written=len(events),
            unmatched_runs=unmatched_runs,
        )
    finally:
        if owns_client:
            await client.aclose()


async def run_forever(
    project_name: str,
    writer: EventWriter,
    *,
    interval_s: float,
    iterations: int | None = None,
    client: AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Poll `poll_once` on a fixed interval. `iterations=None` loops forever; a finite count is
    for tests — the sleep never runs after the final pass."""
    owns_client = client is None
    client = client or AsyncClient()
    try:
        count = 0
        while iterations is None or count < iterations:
            try:
                result = await poll_once(project_name, writer, client=client, sleep=sleep)
                if result.status != "ok":
                    logger.warning("langsmith: poll %s: %s", result.status, result.detail)
            except Exception:
                logger.exception("langsmith: poll pass failed, will retry next interval")
            count += 1
            if iterations is not None and count >= iterations:
                break
            await sleep(interval_s)
    finally:
        if owns_client:
            await client.aclose()
