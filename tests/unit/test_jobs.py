import pytest

from orchestrator.domain import JobStatus
from orchestrator.jobs.service import InvalidTransition


def test_jobs_are_idempotent_and_transitioned(jobs) -> None:
    job = jobs.create_job(kind="global_question", idempotency_key="same")
    same = jobs.create_job(kind="global_question", idempotency_key="same")
    assert same.id == job.id
    claimed = jobs.claim_next()
    assert claimed is not None
    assert claimed.status == JobStatus.RUNNING.value
    jobs.transition(job.id, JobStatus.COMPLETED)
    assert jobs.get(job.id).status == JobStatus.COMPLETED.value


def test_invalid_transition_is_rejected(jobs) -> None:
    job = jobs.create_job(kind="global_question", idempotency_key="invalid")
    with pytest.raises(InvalidTransition):
        jobs.transition(job.id, JobStatus.PUSHING)


def test_push_approval_is_bound_to_head_and_can_be_invalidated(jobs) -> None:
    job = jobs.create_job(kind="implementation", idempotency_key="approval")
    approval = jobs.create_push_approval(job.id, "abc123", "system-pending")
    with pytest.raises(ValueError, match="does not match"):
        jobs.approve_push(job.id, "different", "user")
    approved = jobs.approve_push(job.id, "abc123", "user", "telegram-message")
    assert approved.status == "approved"
    jobs.invalidate_push_approvals(job.id)
    assert jobs.pending_push_approval(job.id) is None

