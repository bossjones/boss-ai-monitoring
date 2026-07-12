"""RED-first tests for ingest/langsmith_poll.py (Phase 5: LangSmith poller, merged view).

respx-mocked httpx transport underneath `langsmith.AsyncClient` — hermetic, no real network or
ambient LANGSMITH_* credentials touched (see the autouse `isolated_langsmith_env` fixture in
conftest.py). See OQ-jsonl-02 for the thread_id<->session_id best-effort join this exercises
(spec RISK #2).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from langsmith import AsyncClient

from boss_ai_monitoring.ingest.langsmith_poll import PollResult, poll_once, run_forever
from boss_ai_monitoring.store.writer import EventWriter

PROJECT = "cc-project"
# Matches conftest.py's LANGSMITH_API_URL — kept as a literal here too since pyrefly's project
# layout only treats `src` as an import root, so tests can't import from a sibling conftest.py.
LANGSMITH_API_URL = "http://testserver"


def _client(api_key: str | None = "test-key") -> AsyncClient:
    return AsyncClient(api_url=LANGSMITH_API_URL, api_key=api_key)


def _peek_rows(db_path: Path, *, where: str = "1=1") -> list[dict[str, Any]]:
    import duckdb

    conn = duckdb.connect(str(db_path))
    try:
        cols = [d[0] for d in conn.execute("SELECT * FROM events LIMIT 0").description]
        rows = conn.execute(f"SELECT * FROM events WHERE {where}").fetchall()
        return [dict(zip(cols, row, strict=True)) for row in rows]
    finally:
        conn.close()


def _seed_local_session(writer: EventWriter, session_id: str) -> None:
    writer.write(
        {
            "event_id": f"otlp:{session_id}:seed",
            "ts": datetime.now(UTC),
            "source": "otlp",
            "event_type": "api_request",
            "session_id": session_id,
        }
    )
    writer.flush()


# ---------------------------------------------------------------------------
# Basic mapping + cursor persistence
# ---------------------------------------------------------------------------


async def test_poll_once_maps_runs_to_events_and_persists_cursor(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    make_langsmith_run: Callable[..., dict[str, Any]],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    start = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
    run = make_langsmith_run(start_time=start, thread_id="sess-a")
    mock_langsmith_runs_pages(respx_mock, [[run]])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        _seed_local_session(writer, "sess-a")
        client = _client()
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()
        writer.flush()

        cursor = writer.get_cursor("langsmith", PROJECT)

    assert result.status == "ok"
    assert result.runs_seen == 1
    assert result.events_written == 1
    assert cursor is not None

    rows = _peek_rows(db_path, where="source = 'langsmith'")
    assert len(rows) == 1
    assert rows[0]["event_id"] == f"langsmith:{run['id']}"
    assert rows[0]["session_id"] == "sess-a"
    assert rows[0]["tokens_input"] == 10
    assert rows[0]["tokens_output"] == 5
    assert rows[0]["cost_usd"] == pytest.approx(0.01)


async def test_poll_once_populates_trace_id_and_run_type_in_payload(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    make_langsmith_run: Callable[..., dict[str, Any]],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    run = make_langsmith_run(
        start_time=datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC), run_type="chain"
    )
    mock_langsmith_runs_pages(respx_mock, [[run]])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        await poll_once(PROJECT, writer, client=client)
        await client.aclose()
        writer.flush()

    rows = _peek_rows(db_path, where="source = 'langsmith'")
    payload = json.loads(rows[0]["payload"])
    assert payload["trace_id"] == run["trace_id"]
    assert payload["run_type"] == "chain"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_poll_once_follows_pagination_cursors(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    make_langsmith_run: Callable[..., dict[str, Any]],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    base = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
    page1 = [make_langsmith_run(start_time=base + timedelta(seconds=i)) for i in range(3)]
    page2 = [make_langsmith_run(start_time=base + timedelta(seconds=10 + i)) for i in range(2)]
    mock_langsmith_runs_pages(respx_mock, [page1, page2])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()
        writer.flush()

    assert result.runs_seen == 5
    assert result.events_written == 5


async def test_poll_once_requests_at_most_100_runs_per_page(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    """The real /runs/query endpoint 400s on a body limit > 100 ("Limit exceeds maximum
    allowed value of 100", observed live 2026-07-12); the async SDK sends our `limit`
    verbatim as the page size, so poll_once must never ask for more."""
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    mock_langsmith_runs_pages(respx_mock, [[]])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        await poll_once(PROJECT, writer, client=client)
        await client.aclose()

    query_requests = [
        call.request for call in respx_mock.calls if call.request.url.path == "/runs/query"
    ]
    assert query_requests
    for request in query_requests:
        assert json.loads(request.content)["limit"] <= 100


async def test_poll_once_requests_oldest_runs_first(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    """/runs/query defaults to newest-first (observed live 2026-07-12), which silently DROPS
    the older backlog whenever a poll hits the 100-run cap: the cursor lands 60s behind the
    newest run and everything older than the fetched page can never match a later window.
    Ascending order makes a capped pass take the OLDEST runs, so the cursor floors the next
    pass and the backlog drains instead."""
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    mock_langsmith_runs_pages(respx_mock, [[]])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        await poll_once(PROJECT, writer, client=client)
        await client.aclose()

    query_requests = [
        call.request for call in respx_mock.calls if call.request.url.path == "/runs/query"
    ]
    assert query_requests
    for request in query_requests:
        assert json.loads(request.content)["order"] == "asc"


async def test_poll_once_empty_page_yields_zero_events(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    mock_langsmith_runs_pages(respx_mock, [[]])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()

    assert result.status == "ok"
    assert result.runs_seen == 0
    assert result.events_written == 0


# ---------------------------------------------------------------------------
# Rate limiting: exponential backoff on 429, bounded retries
# ---------------------------------------------------------------------------


async def test_poll_once_retries_after_429_then_succeeds(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    make_langsmith_run: Callable[..., dict[str, Any]],
    mock_langsmith_runs_sequence: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    run = make_langsmith_run(start_time=datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC))
    mock_langsmith_runs_sequence(
        respx_mock,
        [
            httpx.Response(429, json={"detail": "rate limited"}),
            httpx.Response(200, json={"runs": [run]}),
        ],
    )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        result = await poll_once(PROJECT, writer, client=client, sleep=fake_sleep)
        await client.aclose()

    assert result.status == "ok"
    assert result.events_written == 1
    assert len(sleeps) == 1  # backed off exactly once before the retry succeeded


async def test_poll_once_gives_up_after_max_retries_without_crashing(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    mock_langsmith_runs_sequence: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    mock_langsmith_runs_sequence(
        respx_mock,
        [httpx.Response(429, json={"detail": "rate limited"}) for _ in range(10)],
    )

    async def fake_sleep(seconds: float) -> None:
        return None

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        result = await poll_once(PROJECT, writer, client=client, sleep=fake_sleep, max_retries=3)
        await client.aclose()

    assert result.status == "error"
    assert result.events_written == 0


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


async def test_poll_once_disables_with_no_api_key_and_makes_no_request(
    db_path: Path, respx_mock: Any
) -> None:
    # deliberately no mock registration — any request would raise AllMockedAssertionError
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client(api_key=None)
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()

    assert result.status == "disabled"
    assert result.detail is not None
    assert respx_mock.calls.call_count == 0


async def test_poll_once_disables_on_invalid_api_key_without_crashing(
    db_path: Path, respx_mock: Any
) -> None:
    respx_mock.get(f"{LANGSMITH_API_URL}/sessions", params={"name": PROJECT}).mock(
        return_value=httpx.Response(401, json={"detail": "invalid api key"})
    )

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client(api_key="bad-key")
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()

    assert result.status == "disabled"
    assert result.events_written == 0


# ---------------------------------------------------------------------------
# Unmatched thread_id -> LangSmith-only bucket, never silently merged
# ---------------------------------------------------------------------------


async def test_unmatched_thread_id_leaves_session_id_null_but_keeps_thread_id_in_payload(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    make_langsmith_run: Callable[..., dict[str, Any]],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    run = make_langsmith_run(
        start_time=datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC), thread_id="no-such-session"
    )
    mock_langsmith_runs_pages(respx_mock, [[run]])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        # no local session seeded — "no-such-session" doesn't exist locally
        client = _client()
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()
        writer.flush()

    assert result.unmatched_runs == 1
    rows = _peek_rows(db_path, where="source = 'langsmith'")
    assert rows[0]["session_id"] is None
    payload = json.loads(rows[0]["payload"])
    assert payload["extra"]["metadata"]["thread_id"] == "no-such-session"


async def test_run_with_no_thread_id_is_not_counted_unmatched(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    make_langsmith_run: Callable[..., dict[str, Any]],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    run = make_langsmith_run(start_time=datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC), thread_id=None)
    mock_langsmith_runs_pages(respx_mock, [[run]])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()

    assert result.unmatched_runs == 0


# ---------------------------------------------------------------------------
# Missing token usage never crashes
# ---------------------------------------------------------------------------


async def test_run_with_missing_token_usage_is_still_written(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    make_langsmith_run: Callable[..., dict[str, Any]],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    run = make_langsmith_run(
        start_time=datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC),
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        total_cost=None,
    )
    mock_langsmith_runs_pages(respx_mock, [[run]])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()
        writer.flush()

    assert result.status == "ok"
    rows = _peek_rows(db_path, where="source = 'langsmith'")
    assert rows[0]["tokens_input"] is None
    assert rows[0]["cost_usd"] is None


# ---------------------------------------------------------------------------
# Clock-skewed start_time overlap: cursor overlaps 1 min, dedupes on run id
# ---------------------------------------------------------------------------


async def test_cursor_overlaps_by_one_minute_and_rerun_is_idempotent(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    make_langsmith_run: Callable[..., dict[str, Any]],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    start = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
    run = make_langsmith_run(run_id="11111111-1111-1111-1111-111111111111", start_time=start)
    mock_langsmith_runs_pages(respx_mock, [[run]])

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        await poll_once(PROJECT, writer, client=client)
        await client.aclose()
        writer.flush()
        cursor_after_first = writer.get_cursor("langsmith", PROJECT)

    assert cursor_after_first is not None
    cursor_dt = datetime.fromisoformat(cursor_after_first)
    assert cursor_dt == start - timedelta(seconds=60)

    # second poll starts from that overlapping cursor and sees the SAME run again —
    # the writer's event_id anti-join must make this a no-op, not a duplicate row.
    mock_langsmith_runs_pages(respx_mock, [[run]])
    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        result = await poll_once(PROJECT, writer, client=client)
        await client.aclose()
        writer.flush()

    assert result.runs_seen == 1  # the API re-served it (overlap window)
    rows = _peek_rows(db_path, where="source = 'langsmith'")
    assert len(rows) == 1  # but the store deduped it to exactly one row


# ---------------------------------------------------------------------------
# PollResult defaults
# ---------------------------------------------------------------------------


def test_poll_result_defaults() -> None:
    result = PollResult(status="ok")
    assert result.runs_seen == 0
    assert result.events_written == 0
    assert result.unmatched_runs == 0
    assert result.detail is None


# ---------------------------------------------------------------------------
# run_forever: the polling loop wrapper
# ---------------------------------------------------------------------------


async def test_run_forever_polls_once_per_iteration(
    db_path: Path,
    respx_mock: Any,
    mock_langsmith_project: Callable[..., str],
    make_langsmith_run: Callable[..., dict[str, Any]],
    mock_langsmith_runs_pages: Callable[..., None],
) -> None:
    mock_langsmith_project(respx_mock, project_name=PROJECT)
    run = make_langsmith_run(start_time=datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC))
    mock_langsmith_runs_pages(respx_mock, [[run]])

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client()
        await run_forever(
            PROJECT, writer, interval_s=60, iterations=2, client=client, sleep=fake_sleep
        )
        writer.flush()

    # 2 iterations, 1 sleep between them — the loop never sleeps after the final pass.
    assert sleeps == [60]
    rows = _peek_rows(db_path, where="source = 'langsmith'")
    assert len(rows) == 1  # the single poll pass (first iteration) already ingested the run


async def test_run_forever_survives_a_disabled_pass(db_path: Path) -> None:
    """No API key -> poll_once returns status=disabled, and the loop keeps ticking, not crashing."""
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    with EventWriter(db_path, batch_size=1000, flush_interval_ms=60_000) as writer:
        client = _client(api_key=None)
        await run_forever(
            PROJECT, writer, interval_s=1, iterations=3, client=client, sleep=fake_sleep
        )
        await client.aclose()

    assert calls == [1, 1]
