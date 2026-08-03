from orchestrator.domain import JobStatus


def test_recovery_requeues_interrupted_jobs(jobs) -> None:
    job = jobs.create_job(kind="global_question", idempotency_key="recovery")
    jobs.claim_next()
    assert jobs.get(job.id).status == JobStatus.RUNNING.value
    result = jobs.recover()
    assert result["requeued"] == 1
    assert jobs.get(job.id).status == JobStatus.QUEUED.value

