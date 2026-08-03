from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from orchestrator.database.engine import Database
from orchestrator.database.models import (
    Job,
    JobEvent,
    JobInput,
    JobQuestion,
    Proposal,
    ProposalApproval,
    PushApproval,
    utcnow,
)
from orchestrator.domain import ALLOWED_TRANSITIONS, JobStatus


class InvalidTransition(ValueError):
    pass


class JobService:
    def __init__(self, database: Database, *, approval_ttl_minutes: int = 60) -> None:
        self.database = database
        self.approval_ttl_minutes = approval_ttl_minutes

    def create_job(
        self,
        *,
        kind: str,
        idempotency_key: str,
        project_id: str | None = None,
        repository_id: str | None = None,
        request_text: str | None = None,
        context: dict[str, Any] | None = None,
        status: JobStatus = JobStatus.QUEUED,
    ) -> Job:
        with self.database.session() as session:
            existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
            if existing:
                return existing
            job = Job(
                id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                project_id=project_id,
                repository_id=repository_id,
                kind=kind,
                status=JobStatus.CREATED.value,
                request_text=request_text,
                context=context or {},
            )
            session.add(job)
            session.flush()
            self._event(session, job.id, "JOB_CREATED", {"kind": kind})
            if status != JobStatus.CREATED:
                self._transition(session, job, status, {})
            session.commit()
            return job

    def get(self, job_id: str) -> Job | None:
        with self.database.session() as session:
            return session.get(Job, job_id)

    def transition(self, job_id: str, status: JobStatus, payload: dict[str, Any] | None = None) -> Job:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            self._transition(session, job, status, payload or {})
            session.commit()
            return job

    def _transition(self, session: Any, job: Job, status: JobStatus, payload: dict[str, Any]) -> None:
        current = JobStatus(job.status)
        if status != current and status not in ALLOWED_TRANSITIONS[current]:
            raise InvalidTransition(f"{current.value} -> {status.value} is not allowed")
        job.status = status.value
        job.updated_at = utcnow()
        self._event(session, job.id, f"JOB_{status.value.upper()}", payload)

    def add_event(self, job_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        with self.database.session() as session:
            self._event(session, job_id, event_type, payload or {})
            session.commit()

    @staticmethod
    def _event(session: Any, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        session.add(JobEvent(job_id=job_id, event_type=event_type, payload=payload))

    def claim_next(self) -> Job | None:
        with self.database.session() as session:
            job = session.scalar(
                select(Job).where(Job.status == JobStatus.QUEUED.value).order_by(Job.created_at).limit(1)
            )
            if not job:
                return None
            result = session.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == JobStatus.QUEUED.value)
                .values(status=JobStatus.RUNNING.value, updated_at=utcnow())
            )
            if getattr(result, "rowcount", 0) != 1:
                session.rollback()
                return None
            self._event(session, job.id, "JOB_CLAIMED", {})
            session.commit()
            return session.get(Job, job.id)

    def add_input(
        self,
        job_id: str,
        text: str,
        *,
        source: str = "telegram",
        telegram_user_id: str | None = None,
        telegram_message_id: str | None = None,
    ) -> None:
        with self.database.session() as session:
            session.add(
                JobInput(
                    job_id=job_id,
                    source=source,
                    text=text,
                    telegram_user_id=telegram_user_id,
                    telegram_message_id=telegram_message_id,
                )
            )
            session.commit()

    def update_context(self, job_id: str, values: dict[str, Any]) -> None:
        with self.database.session() as session:
            job = session.get(Job, job_id)
            if not job:
                raise KeyError(job_id)
            job.context = {**(job.context or {}), **values}
            job.updated_at = utcnow()
            session.commit()

    def pending_proposals(self, job_id: str) -> list[Proposal]:
        with self.database.session() as session:
            return list(session.scalars(
                select(Proposal).where(Proposal.job_id == job_id, Proposal.status == "pending")
            ).all())

    def add_question(
        self,
        job_id: str,
        question_id: str,
        question: str,
        question_type: str,
        options: list[dict[str, Any]] | None = None,
        required: bool = True,
    ) -> JobQuestion:
        with self.database.session() as session:
            item = JobQuestion(
                id=str(uuid.uuid4()),
                job_id=job_id,
                question_id=question_id,
                question=question,
                question_type=question_type,
                options=options or [],
                required=required,
            )
            session.add(item)
            session.commit()
            return item

    def approve_proposal(self, proposal_id: str, job_id: str, base_sha: str, user_id: str, message_id: str | None) -> None:
        with self.database.session() as session:
            proposal = session.get(Proposal, proposal_id)
            if not proposal or proposal.job_id != job_id or proposal.status != "pending":
                raise ValueError("Proposal is not pending for this job")
            proposal.status = "approved"
            session.add(
                ProposalApproval(
                    proposal_id=proposal_id,
                    job_id=job_id,
                    base_sha=base_sha,
                    telegram_user_id=user_id,
                    telegram_message_id=message_id,
                )
            )
            session.commit()

    def create_push_approval(self, job_id: str, head_sha: str, user_id: str, message_id: str | None = None) -> PushApproval:
        with self.database.session() as session:
            session.query(PushApproval).filter(
                PushApproval.job_id == job_id, PushApproval.status == "pending"
            ).update({"status": "invalidated", "invalidated_at": utcnow()})
            approval = PushApproval(
                job_id=job_id,
                approved_head_sha=head_sha,
                approved_by_telegram_user_id=user_id,
                approval_message_id=message_id,
                status="pending",
                expires_at=utcnow() + timedelta(minutes=self.approval_ttl_minutes),
            )
            session.add(approval)
            session.commit()
            return approval

    def approve_push(self, job_id: str, head_sha: str, user_id: str, message_id: str | None = None) -> PushApproval:
        with self.database.session() as session:
            approval = session.scalar(
                select(PushApproval)
                .where(PushApproval.job_id == job_id, PushApproval.status == "pending")
                .order_by(PushApproval.created_at.desc())
            )
            if not approval:
                raise ValueError("No pending push approval")
            if approval.approved_head_sha != head_sha:
                raise ValueError("Approval does not match current HEAD")
            if approval.expires_at <= utcnow():
                approval.status = "expired"
                session.commit()
                raise ValueError("Push approval has expired")
            approval.status = "approved"
            approval.approved_by_telegram_user_id = user_id
            approval.approval_message_id = message_id or approval.approval_message_id
            session.commit()
            return approval

    def invalidate_push_approvals(self, job_id: str) -> None:
        with self.database.session() as session:
            session.query(PushApproval).filter(
                PushApproval.job_id == job_id, PushApproval.status == "pending"
            ).update({"status": "invalidated", "invalidated_at": utcnow()})
            session.commit()

    def pending_push_approval(self, job_id: str) -> PushApproval | None:
        with self.database.session() as session:
            return session.scalar(
                select(PushApproval)
                .where(PushApproval.job_id == job_id, PushApproval.status == "pending")
                .order_by(PushApproval.created_at.desc())
            )

    def recover(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.database.session() as session:
            jobs = session.scalars(
                select(Job).where(
                    Job.status.in_([
                        JobStatus.RUNNING.value,
                        JobStatus.PREPARING_WORKTREE.value,
                        JobStatus.IMPLEMENTING.value,
                        JobStatus.VALIDATING.value,
                        JobStatus.PUSHING.value,
                        JobStatus.CREATING_DRAFT_PR.value,
                    ])
                )
            ).all()
            for job in jobs:
                previous_status = job.status
                job.status = JobStatus.QUEUED.value
                job.error = "Recovered after application restart"
                self._event(session, job.id, "JOB_RECOVERED", {"previous_status": previous_status})
                counts["requeued"] = counts.get("requeued", 0) + 1
            session.commit()
        return counts
