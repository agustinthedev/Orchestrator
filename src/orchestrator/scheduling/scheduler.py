from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from orchestrator.config.models import AppConfig, ScheduleConfig
from orchestrator.database.engine import Database
from orchestrator.database.models import Schedule
from orchestrator.jobs.service import JobService

WorkflowFactory = Callable[[ScheduleConfig], Awaitable[None]]


class SchedulerService:
    def __init__(self, config: AppConfig, database: Database, jobs: JobService, workflow_factory: WorkflowFactory) -> None:
        self.config = config
        self.database = database
        self.jobs = jobs
        self.workflow_factory = workflow_factory
        self.scheduler = AsyncIOScheduler(timezone=config.application.timezone)

    def load(self) -> None:
        with self.database.session() as session:
            for item in self.config.schedules:
                project = self.config.project(item.project)
                existing = session.get(Schedule, item.id)
                if existing:
                    existing.project_id = project.id
                    existing.workflow = item.workflow
                    existing.cron = item.cron
                    existing.timezone = item.timezone or self.config.application.timezone
                    existing.enabled = item.enabled
                    existing.parameters = item.parameters
                    existing.misfire_grace_time = item.misfire_grace_time
                    existing.coalesce = item.coalesce
                    existing.max_instances = item.max_instances
                else:
                    session.add(
                        Schedule(
                            id=item.id,
                            project_id=project.id,
                            workflow=item.workflow,
                            cron=item.cron,
                            timezone=item.timezone or self.config.application.timezone,
                            enabled=item.enabled,
                            parameters=item.parameters,
                            misfire_grace_time=item.misfire_grace_time,
                            coalesce=item.coalesce,
                            max_instances=item.max_instances,
                        )
                    )
            session.commit()

    def start(self) -> None:
        self.load()
        for item in self.config.schedules:
            if not item.enabled:
                continue
            trigger = self._trigger(item)
            self.scheduler.add_job(
                self._run_schedule,
                trigger=trigger,
                id=item.id,
                replace_existing=True,
                coalesce=item.coalesce,
                max_instances=item.max_instances,
                misfire_grace_time=item.misfire_grace_time,
                args=[item],
            )
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return bool(self.scheduler.running)

    def _trigger(self, item: ScheduleConfig) -> CronTrigger:
        parts = item.cron.split()
        if len(parts) != 5:
            raise ValueError(f"Schedule {item.id} must use a five-field cron expression")
        minute, hour, day, month, day_of_week = parts
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=item.timezone or self.config.application.timezone,
        )

    async def _run_schedule(self, item: ScheduleConfig) -> None:
        idempotency = f"schedule:{item.id}:{datetime.utcnow().strftime('%Y%m%d%H%M')}"
        self.jobs.create_job(
            kind=item.workflow,
            idempotency_key=idempotency,
            project_id=item.project,
            request_text=f"Scheduled workflow: {item.workflow}",
            context={"schedule_id": item.id, "parameters": item.parameters},
        )
        await self.workflow_factory(item)
