from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from orchestrator.database.models import Job
from orchestrator.domain import JobStatus
from orchestrator.jobs.service import JobService
from orchestrator.observability.logging import get_logger

logger = get_logger(__name__)


JobHandler = Callable[[Job], Awaitable[None]]


class WorkerPool:
    def __init__(self, jobs: JobService, handler: JobHandler, *, count: int = 1, poll_interval: float = 2) -> None:
        self.jobs = jobs
        self.handler = handler
        self.count = count
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._stop.clear()
        self._tasks = [asyncio.create_task(self._loop(index), name=f"orchestrator-worker-{index}") for index in range(self.count)]

    async def stop(self) -> None:
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, index: int) -> None:
        while not self._stop.is_set():
            self.jobs.expire_pending_interactions()
            job = self.jobs.claim_next()
            if not job:
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                await self.handler(job)
            except Exception as exc:
                logger.exception("Worker failed", extra={"job_id": job.id, "event_type": "WORKER_FAILURE"})
                try:
                    self.jobs.transition(job.id, JobStatus.FAILED, {"error": str(exc)})
                except Exception:
                    logger.exception("Could not persist worker failure", extra={"job_id": job.id})
