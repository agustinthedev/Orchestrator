import pytest

from orchestrator.domain import JobStatus
from orchestrator.telegram.gateway import InboundMessage, ReplyMarkup, TelegramGateway


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
    reactions: list[tuple[str, str, str]] = []

    async def creator(intent: str, project_id: str | None, repository_id: str | None, text: str, update_id: str) -> str:
        created.append(intent)
        return "job-1"

    async def reactor(chat_id: str, message_id: str, emoji: str) -> None:
        reactions.append((chat_id, message_id, emoji))

    gateway = TelegramGateway(config, database, jobs, reactor=reactor, job_creator=creator)
    reply = await gateway.handle(InboundMessage("update-1", "7", "42", "message-1", "How does the scheduler work?"))
    assert reply is None
    assert created == ["GLOBAL_QUESTION"]
    assert reactions == [("7", "message-1", "👀")]


@pytest.mark.asyncio
async def test_confirmation_without_reply_metadata_reuses_pending_job(database, jobs, seeded_config, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "7")
    pending = jobs.create_job(
        kind="implementation",
        idempotency_key="pending-voice-confirmation",
        project_id="project",
        request_text="Update the README",
        status=JobStatus.AWAITING_INPUT,
    )
    with database.session() as session:
        from orchestrator.database.models import TelegramOutboundMessage

        session.add(
            TelegramOutboundMessage(
                chat_id="7",
                message_id="confirmation-1",
                message_type="voice_confirmation",
                text="Entendí...",
                project_id="project",
                job_id=pending.id,
            )
        )
        session.commit()

    gateway = TelegramGateway(seeded_config, database, jobs)
    reply = await gateway.handle(InboundMessage("confirmation-audio", "7", "42", "message-2", "Exacto, eso mismo.", voice_file_id="voice-2"))

    assert reply is not None
    assert jobs.get(pending.id).status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_unknown_message_does_not_create_worker_job(database, jobs, config, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "7")
    created: list[str] = []

    async def creator(intent: str, project_id: str | None, repository_id: str | None, text: str, update_id: str) -> str:
        created.append(intent)
        return "job-unknown"

    gateway = TelegramGateway(config, database, jobs, job_creator=creator)
    reply = await gateway.handle(InboundMessage("unknown-1", "7", "42", "message-3", "Exacto, eso mismo."))

    assert reply is not None
    assert created == []


@pytest.mark.asyncio
async def test_voice_mutation_requires_confirmation(database, jobs, seeded_config, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "7")
    sent_options: list[tuple[str, str, str | None, ReplyMarkup | None]] = []

    async def sender_with_options(chat_id: str, text: str, parse_mode: str | None, reply_markup: ReplyMarkup | None) -> str:
        sent_options.append((chat_id, text, parse_mode, reply_markup))
        return "voice-confirmation-message"

    gateway = TelegramGateway(seeded_config, database, jobs, sender_with_options=sender_with_options)
    first = await gateway.handle(InboundMessage("voice-1", "7", "42", "message-voice", "Add a feature in project", voice_file_id="voice-file"))
    assert first is not None
    assert sent_options[0][2] == "HTML"
    assert "<blockquote>Add a feature in project</blockquote>" in sent_options[0][1]
    with database.session() as session:
        from sqlalchemy import select

        from orchestrator.database.models import Job, TelegramOutboundMessage

        outbound = session.scalar(select(TelegramOutboundMessage).where(TelegramOutboundMessage.message_type == "voice_confirmation"))
        assert outbound is not None
        pending = session.get(Job, outbound.job_id)
        assert pending is not None
        assert pending.status == "awaiting_input"
    assert sent_options[0][3] == [[("Confirmar", f"confirm_job:{pending.id}"), ("Cancelar", f"cancel_job:{pending.id}")]]
    second = await gateway.handle_callback("callback-confirm", "confirm_job:" + pending.id, user_id="42", chat_id="7", message_id=outbound.message_id)
    assert second is not None
    assert jobs.get(pending.id).status == "queued"
