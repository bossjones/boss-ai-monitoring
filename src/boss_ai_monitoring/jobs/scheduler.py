"""Trailing-job asyncio scheduler (Phase 8, jobs.md).

Runs the three trailing quality jobs off the critical path — never blocking ingest or the agent
(watcher's lesson, shared.md). Per-job enable/disable comes from `JobsSettings`; a crashing job is
isolated so it never takes down the scheduler or its siblings; the last run's status per job is
kept in memory here (Wave 3 persists it to the store; the provenance footer is web's rendering of
that data — see the BL entry filed for the query/shape).

Wave 1: the shape only. Job callables are injected — Wave 3 wires real callables that read from
`connect_read_only()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from boss_ai_monitoring.config import JobsSettings

JobCallable = Callable[[], object] | Callable[[], Awaitable[object]]

_DEFAULT_ENABLE_FLAG_BY_NAME: dict[str, str] = {
    "correction_scan": "correction_scan_enabled",
    "drift_check": "drift_check_enabled",
    "error_classification": "error_classification_enabled",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class JobDefinition:
    """One trailing job: a name, whether it's enabled, and the callable that runs it."""

    name: str
    enabled: bool
    func: JobCallable


@dataclass(frozen=True)
class JobRunResult:
    """The outcome of one job run — this is what gets persisted and shown in the footer."""

    name: str
    status: Literal["ok", "error"]
    started_at: datetime
    finished_at: datetime
    error: str | None = None
    result: object | None = None


class JobScheduler:
    """Runs enabled jobs periodically, isolating crashes and jittering the sleep interval."""

    def __init__(
        self,
        jobs: Iterable[JobDefinition],
        *,
        interval_s: float,
        jitter_s: float = 0.0,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[float, float], float] = random.uniform,
        persist: Callable[[JobRunResult], None] | None = None,
    ) -> None:
        self._jobs = list(jobs)
        self._interval_s = interval_s
        self._jitter_s = jitter_s
        self._clock = clock
        self._sleep = sleep
        self._rand = rand
        self._persist = persist
        self._last_status: dict[str, JobRunResult] = {}

    @classmethod
    def from_settings(
        cls,
        settings: JobsSettings,
        callables: dict[str, JobCallable],
        *,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[float, float], float] = random.uniform,
        persist: Callable[[JobRunResult], None] | None = None,
    ) -> JobScheduler:
        """Build `JobDefinition`s from a name->callable mapping, gated by settings enable flags."""
        jobs = [
            JobDefinition(
                name=name,
                enabled=getattr(settings, _DEFAULT_ENABLE_FLAG_BY_NAME.get(name, ""), True),
                func=func,
            )
            for name, func in callables.items()
        ]
        return cls(
            jobs,
            interval_s=settings.interval_s,
            jitter_s=settings.jitter_s,
            clock=clock,
            sleep=sleep,
            rand=rand,
            persist=persist,
        )

    async def run_once(self) -> list[JobRunResult]:
        """Run every enabled job once. A disabled job is skipped entirely — no result, no status.

        A job that raises never propagates: its failure is isolated into an "error" `JobRunResult`
        so the rest of the pass — and the scheduler itself — keeps going.
        """
        results: list[JobRunResult] = []
        for job in self._jobs:
            if not job.enabled:
                continue
            started_at = self._clock()
            try:
                # The live jobs are SYNCHRONOUS DuckDB scans (drift check, correction scan, error
                # classification) over the whole events table. Called inline they block the event
                # loop, and with it the dashboard and the OTLP receiver. Async jobs still run
                # inline — only the blocking ones are pushed to a worker thread.
                if asyncio.iscoroutinefunction(job.func):
                    outcome = await job.func()
                else:
                    outcome = await asyncio.to_thread(job.func)
                result = JobRunResult(
                    name=job.name,
                    status="ok",
                    started_at=started_at,
                    finished_at=self._clock(),
                    result=outcome,
                )
            except Exception as exc:
                result = JobRunResult(
                    name=job.name,
                    status="error",
                    started_at=started_at,
                    finished_at=self._clock(),
                    error=str(exc),
                )
            self._last_status[job.name] = result
            results.append(result)
            if self._persist is not None:
                with contextlib.suppress(Exception):  # a broken persist sink must not lose the run
                    self._persist(result)
        return results

    def last_status(self, name: str) -> JobRunResult | None:
        """The most recent run result for `name`, or `None` if it never ran (e.g. disabled)."""
        return self._last_status.get(name)

    async def run_forever(self, *, iterations: int | None = None) -> None:
        """Loop `run_once()`, sleeping `interval_s` + jitter between passes.

        `iterations=None` loops forever; a finite count is for tests — the sleep never runs after
        the final pass.
        """
        count = 0
        while iterations is None or count < iterations:
            await self.run_once()
            count += 1
            if iterations is not None and count >= iterations:
                break
            jitter = self._rand(0, self._jitter_s) if self._jitter_s else 0.0
            await self._sleep(self._interval_s + jitter)
