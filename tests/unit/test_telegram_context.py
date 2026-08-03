import os

import pytest

from orchestrator.telegram.gateway import InboundMessage, TelegramGateway


@pytest.mark.asyncio
async def test_reply_context_resolves_exact_outbound_message(database, jobs, seeded_config, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "7")
    gateway = TelegramGateway(seeded_config, database, jobs)
    job = jobs.create_job(kind="implementation", idempotency_key="telegram-job", project_id="project")
    message_id = await gateway.send("Change ready", chat_id="7", message_type="push_approval", project_id="project", job_id=job.id, head_sha="head1")
    context = gateway.resolve_reply_context("7", message_id)
    assert context is not None
    assert context.job_id == job.id
    assert context.head_sha == "head1"


@pytest.mark.asyncio
async def test_natural_message_creates_global_job(database, jobs, config, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "7")
    created: list[str] = []

    async def creator(intent: str, project_id: str | None, repository_id: str | None, text: str) -> str:
        created.append(intent)
        return "job-1"

    gateway = TelegramGateway(config, database, jobs, job_creator=creator)
    reply = await gateway.handle(InboundMessage("update-1", "7", "42", "message-1", "How does the scheduler work?"))
    assert reply is not None
    assert created == ["GLOBAL_QUESTION"]
