"""RED-FIRST: asyncio trailing-job scheduler (Phase 8, jobs.md).

Shape only in Wave 1 — job callables are injected fixtures, no live DB. Must prove: per-job
enable/disable from `JobsSettings`, jitter on the sleep interval, crash isolation (one failing job
never kills the scheduler or its siblings), and an in-memory last-run status per job (the surface
Wave 3 persists and the provenance footer reads — see BL entry filed against 🖥 web).
"""

from __future__ import annotations

from datetime import UTC, datetime

from boss_ai_monitoring.config import JobsSettings
from boss_ai_monitoring.jobs.scheduler import JobCallable, JobDefinition, JobRunResult, JobScheduler


def _clock_sequence(*timestamps: datetime):
    it = iter(timestamps)

    def _clock() -> datetime:
        return next(it)

    return _clock


async def test_run_once_records_ok_status_for_a_successful_sync_job() -> None:
    scheduler = JobScheduler(
        [JobDefinition(name="corrections", enabled=True, func=lambda: "done")],
        interval_s=60,
        clock=_clock_sequence(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 1, 0, 0, 1, tzinfo=UTC),
        ),
    )

    results = await scheduler.run_once()

    assert len(results) == 1
    assert results[0].name == "corrections"
    assert results[0].status == "ok"
    assert results[0].result == "done"
    assert scheduler.last_status("corrections") is results[0]


async def test_run_once_supports_async_job_callables() -> None:
    async def _job() -> str:
        return "async-done"

    scheduler = JobScheduler([JobDefinition(name="drift", enabled=True, func=_job)], interval_s=60)

    results = await scheduler.run_once()

    assert results[0].status == "ok"
    assert results[0].result == "async-done"


async def test_disabled_job_is_skipped_entirely() -> None:
    ran = []

    def _job() -> None:
        ran.append("ran")

    scheduler = JobScheduler([JobDefinition(name="drift", enabled=False, func=_job)], interval_s=60)

    results = await scheduler.run_once()

    assert results == []
    assert ran == []
    assert scheduler.last_status("drift") is None


async def test_crash_in_one_job_does_not_kill_the_scheduler_or_sibling_jobs() -> None:
    def _boom() -> None:
        raise RuntimeError("kaboom")

    def _ok() -> str:
        return "fine"

    scheduler = JobScheduler(
        [
            JobDefinition(name="broken", enabled=True, func=_boom),
            JobDefinition(name="healthy", enabled=True, func=_ok),
        ],
        interval_s=60,
    )

    results = await scheduler.run_once()

    by_name = {r.name: r for r in results}
    assert by_name["broken"].status == "error"
    assert "kaboom" in (by_name["broken"].error or "")
    assert by_name["healthy"].status == "ok"
    assert by_name["healthy"].result == "fine"
    broken_status = scheduler.last_status("broken")
    healthy_status = scheduler.last_status("healthy")
    assert broken_status is not None
    assert healthy_status is not None
    assert broken_status.status == "error"
    assert healthy_status.status == "ok"


async def test_run_once_isolates_crash_in_async_job_too() -> None:
    async def _boom() -> None:
        raise ValueError("async kaboom")

    scheduler = JobScheduler(
        [JobDefinition(name="broken", enabled=True, func=_boom)], interval_s=60
    )

    results = await scheduler.run_once()

    assert results[0].status == "error"
    assert "async kaboom" in (results[0].error or "")


async def test_from_settings_builds_job_definitions_from_enable_flags() -> None:
    settings = JobsSettings(
        correction_scan_enabled=True,
        drift_check_enabled=False,
        error_classification_enabled=True,
        interval_s=120,
        jitter_s=10,
    )
    ran: list[str] = []
    callables: dict[str, JobCallable] = {
        "correction_scan": lambda: ran.append("correction_scan"),
        "drift_check": lambda: ran.append("drift_check"),
        "error_classification": lambda: ran.append("error_classification"),
    }

    scheduler = JobScheduler.from_settings(settings, callables)
    await scheduler.run_once()

    assert set(ran) == {"correction_scan", "error_classification"}
    assert scheduler.last_status("drift_check") is None


async def test_run_forever_sleeps_interval_plus_jitter_between_passes() -> None:
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    scheduler = JobScheduler(
        [JobDefinition(name="corrections", enabled=True, func=lambda: None)],
        interval_s=60,
        jitter_s=10,
        sleep=_fake_sleep,
        rand=lambda lo, hi: 5.0,
    )

    await scheduler.run_forever(iterations=3)

    # 3 passes -> 2 sleeps between them (no trailing sleep after the last pass)
    assert sleep_calls == [65.0, 65.0]


async def test_persist_hook_is_called_with_the_result_after_an_ok_run() -> None:
    persisted = []

    scheduler = JobScheduler(
        [JobDefinition(name="corrections", enabled=True, func=lambda: "done")],
        interval_s=60,
        persist=persisted.append,
    )

    results = await scheduler.run_once()

    assert persisted == results
    assert persisted[0].status == "ok"


async def test_persist_hook_is_called_with_the_result_after_an_error_run() -> None:
    persisted = []

    def _boom() -> None:
        raise RuntimeError("kaboom")

    scheduler = JobScheduler(
        [JobDefinition(name="broken", enabled=True, func=_boom)],
        interval_s=60,
        persist=persisted.append,
    )

    await scheduler.run_once()

    assert len(persisted) == 1
    assert persisted[0].status == "error"


async def test_persist_hook_is_not_called_for_a_disabled_job() -> None:
    persisted = []

    scheduler = JobScheduler(
        [JobDefinition(name="drift", enabled=False, func=lambda: None)],
        interval_s=60,
        persist=persisted.append,
    )

    await scheduler.run_once()

    assert persisted == []


async def test_persist_hook_failure_does_not_crash_the_scheduler_or_lose_last_status() -> None:
    def _boom_on_persist(result: JobRunResult) -> None:
        raise RuntimeError("persist boom")

    scheduler = JobScheduler(
        [JobDefinition(name="corrections", enabled=True, func=lambda: "done")],
        interval_s=60,
        persist=_boom_on_persist,
    )

    results = await scheduler.run_once()

    assert results[0].status == "ok"
    status = scheduler.last_status("corrections")
    assert status is not None
    assert status.status == "ok"


async def test_run_forever_runs_zero_jitter_when_jitter_s_is_zero() -> None:
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    scheduler = JobScheduler(
        [JobDefinition(name="corrections", enabled=True, func=lambda: None)],
        interval_s=30,
        jitter_s=0,
        sleep=_fake_sleep,
        rand=lambda lo, hi: (_ for _ in ()).throw(AssertionError("rand should not be called")),
    )

    await scheduler.run_forever(iterations=2)

    assert sleep_calls == [30.0]
