from __future__ import annotations

import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import escape
from typing import Any

from sqlalchemy import select

from orchestrator.config.models import AppConfig
from orchestrator.database.engine import Database
from orchestrator.database.models import (
    ConversationThread,
    Job,
    TelegramInboundMessage,
    TelegramOutboundMessage,
    utcnow,
)
from orchestrator.domain import Intent, JobStatus
from orchestrator.intent_classifier import (
    IntentClassification,
    IntentClassifier,
    NullIntentClassifier,
)
from orchestrator.jobs.service import JobService
from orchestrator.observability.logging import get_logger
from orchestrator.transcription.adapters import NullTranscriber, Transcriber

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReplyContext:
    job_id: str | None
    project_id: str | None
    repository_id: str | None
    interaction_id: str | None
    thread_id: str | None
    phase: str | None
    branch_name: str | None
    head_sha: str | None


@dataclass(frozen=True)
class InboundMessage:
    update_id: str
    chat_id: str
    user_id: str
    message_id: str
    text: str | None = None
    reply_to_message_id: str | None = None
    voice_file_id: str | None = None
    intent_override: Intent | None = None
    acknowledged: bool = False


Sender = Callable[[str, str], Awaitable[str]]
ReplyMarkup = list[list[tuple[str, str]]]
SenderWithOptions = Callable[[str, str, str | None, ReplyMarkup | None], Awaitable[str]]
Reactor = Callable[[str, str, str], Awaitable[None]]
JobCreator = Callable[[str, str | None, str | None, str, str], Awaitable[str]]
ACK_REACTION = "👀"


class TelegramGateway:
    """Telegram adapter. The core handler accepts plain data so tests do not need Telegram credentials."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        jobs: JobService,
        *,
        transcriber: Transcriber | None = None,
        sender: Sender | None = None,
        sender_with_options: SenderWithOptions | None = None,
        reactor: Reactor | None = None,
        job_creator: JobCreator | None = None,
        classifier: IntentClassifier | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.jobs = jobs
        self.transcriber = transcriber or NullTranscriber()
        self.sender = sender
        self.sender_with_options = sender_with_options
        self.reactor = reactor
        self.job_creator = job_creator
        self.classifier = classifier or NullIntentClassifier()
        self.connected = False

    def authorized(self, user_id: str, chat_id: str) -> bool:
        allowed_users = _csv_env(self.config.telegram.allowed_user_ids_env)
        allowed_chats = _csv_env(self.config.telegram.allowed_chat_ids_env)
        if allowed_users and user_id not in allowed_users:
            return False
        if allowed_chats and chat_id not in allowed_chats:
            return False
        return bool(allowed_users or allowed_chats)

    def resolve_reply_context(self, chat_id: str, message_id: str | None) -> ReplyContext | None:
        if not message_id:
            return None
        with self.database.session() as session:
            message = session.scalar(
                select(TelegramOutboundMessage).where(
                    TelegramOutboundMessage.chat_id == chat_id,
                    TelegramOutboundMessage.message_id == message_id,
                )
            )
            if not message:
                return None
            return ReplyContext(
                message.job_id,
                message.project_id,
                message.repository_id,
                message.interaction_id,
                message.thread_id,
                message.execution_phase,
                message.branch_name,
                message.head_sha,
            )

    def resolve_pending_input_context(self, chat_id: str) -> ReplyContext | None:
        """Resolve the latest pending confirmation when Telegram omitted reply metadata."""
        with self.database.session() as session:
            message = session.scalar(
                select(TelegramOutboundMessage)
                .where(
                    TelegramOutboundMessage.chat_id == chat_id,
                    TelegramOutboundMessage.message_type.in_(["voice_confirmation", "input_request"]),
                )
                .order_by(TelegramOutboundMessage.created_at.desc())
            )
            if not message or not message.job_id:
                return None
            job = session.get(Job, message.job_id)
            if not job or job.status != JobStatus.AWAITING_INPUT.value:
                return None
            return ReplyContext(
                message.job_id,
                message.project_id,
                message.repository_id,
                message.interaction_id,
                message.thread_id,
                message.execution_phase,
                message.branch_name,
                message.head_sha,
            )

    async def handle(self, message: InboundMessage) -> str | None:
        if not self.authorized(message.user_id, message.chat_id):
            logger.warning("Rejected unauthorized Telegram message", extra={"event_type": "TELEGRAM_REJECTED"})
            return None
        context = self.resolve_reply_context(message.chat_id, message.reply_to_message_id)
        text = message.text
        if message.voice_file_id and not text:
            text = await self.transcribe_voice(message.voice_file_id)
        if not text:
            return await self.send("No pude interpretar ese mensaje.", chat_id=message.chat_id, message_type="clarification")
        if not context and self._is_pending_input_response(text):
            context = self.resolve_pending_input_context(message.chat_id)
        self._persist_inbound(message, text)
        state = None
        current_job = None
        if context and context.job_id:
            current_job = self.jobs.get(context.job_id)
            state = current_job.status if current_job else None
        if message.intent_override is not None:
            classification = IntentClassification(
                intent=message.intent_override,
                project_id=context.project_id if context else None,
                confidence=1,
                reason="explicit_callback",
            )
        elif state == JobStatus.AWAITING_INPUT.value:
            classification = IntentClassification(
                intent=Intent.UNKNOWN,
                project_id=context.project_id if context else None,
                confidence=1,
                reason="pending_input",
            )
        else:
            classification = await self.classifier.classify(
                text,
                project_ids=[project.id for project in self.config.projects],
                state=state,
                has_reply_context=context is not None,
            )
        intent = classification.intent
        if intent == Intent.REQUEST_REVISION and not (current_job and current_job.worktree_path):
            intent = Intent.FEATURE_REQUEST
            classification = classification.model_copy(
                update={"intent": intent, "reason": "no_revision_worktree"}
            )
        self._set_intent(message.update_id, intent.value)
        if context and context.job_id and state == JobStatus.AWAITING_INPUT.value:
            if re.search(r"\b(cancel|cancelar|cancelo|stop|abort|no|rechazo)\b", text.casefold()):
                self.jobs.transition(context.job_id, JobStatus.CANCELLED, {"reason": "user_cancelled_input"})
                response = await self.send("Cancelé el job pendiente.", chat_id=message.chat_id, job_id=context.job_id, message_type="job_cancelled")
                await self._react_to_message(message, job_id=context.job_id)
                return response
            self.jobs.add_input(context.job_id, text, telegram_user_id=message.user_id, telegram_message_id=message.message_id)
            answers = list(current_job.context.get("user_answers", [])) if current_job else []
            self.jobs.update_context(context.job_id, {"user_answers": [*answers, text], "voice_confirmation_pending": False})
            self.jobs.transition(context.job_id, JobStatus.INPUT_RECEIVED, {"telegram_user_id": message.user_id})
            self.jobs.transition(context.job_id, JobStatus.QUEUED, {"reason": "user_input_received"})
            response = await self.send("Respuesta registrada; reanudaré el job con este contexto.", chat_id=message.chat_id, job_id=context.job_id, message_type="input_received")
            await self._react_to_message(message, job_id=context.job_id)
            return response
        if message.voice_file_id and intent in {
            Intent.FEATURE_REQUEST,
            Intent.FIX_REQUEST,
            Intent.REFACTOR_REQUEST,
            Intent.TEST_REQUEST,
            Intent.REQUEST_REVISION,
        }:
            project_id = self._resolve_project(context, classification.project_id, text)
            repository_id = context.repository_id if context else None
            is_revision = bool(intent == Intent.REQUEST_REVISION and context and context.job_id)
            pending = self.jobs.create_job(
                kind="implementation" if intent in {
                    Intent.FEATURE_REQUEST,
                    Intent.FIX_REQUEST,
                    Intent.REFACTOR_REQUEST,
                    Intent.TEST_REQUEST,
                    Intent.REQUEST_REVISION,
                } else intent.value,
                idempotency_key=f"voice-confirmation:{message.update_id}",
                project_id=project_id,
                repository_id=repository_id,
                request_text=text,
                context={
                    "voice_confirmation_pending": True,
                    "transcript": text,
                    "revision_request": text if is_revision else None,
                    "target_job_id": context.job_id if is_revision and context else None,
                },
                status=JobStatus.AWAITING_INPUT,
            )
            response = await self.send(
                f"<b>Entendí:</b>\n\n<blockquote>{escape(text)}</blockquote>\n\n<b>Esto puede modificar archivos.</b>\nUsa un botón o responde a este mensaje para confirmar o cancelar.",
                chat_id=message.chat_id,
                project_id=project_id,
                repository_id=repository_id,
                job_id=pending.id,
                message_type="voice_confirmation",
                execution_phase="awaiting_input",
                parse_mode="HTML",
                reply_markup=[
                    [("Confirmar", f"confirm_job:{pending.id}"), ("Cancelar", f"cancel_job:{pending.id}")]
                ],
            )
            await self._react_to_message(message, job_id=pending.id)
            return response
        if intent == Intent.APPROVE_PUSH and context and context.job_id and context.head_sha:
            try:
                self.jobs.approve_push(context.job_id, context.head_sha, message.user_id, message.message_id)
                self.jobs.update_context(context.job_id, {"push_requested": True})
                self.jobs.transition(context.job_id, JobStatus.PUSH_APPROVED, {"telegram_user_id": message.user_id})
                self.jobs.transition(context.job_id, JobStatus.QUEUED, {"reason": "explicit_push_approval"})
                return await self.send("Push aprobado para el HEAD exacto. La cola iniciará el push y creará el PR draft.", chat_id=message.chat_id, job_id=context.job_id, message_type="push_approved")
            except ValueError as exc:
                return await self.send(f"No puedo autorizar el push: {exc}", chat_id=message.chat_id, job_id=context.job_id, message_type="approval_error")
        if intent == Intent.REJECT_PUSH and context and context.job_id:
            self.jobs.invalidate_push_approvals(context.job_id)
            self.jobs.transition(context.job_id, JobStatus.CANCELLED, {"reason": "user_rejected_push"})
            return await self.send("Push rechazado; no se enviará ningún cambio.", chat_id=message.chat_id, job_id=context.job_id, message_type="push_rejected")
        if intent == Intent.REQUEST_REVISION and context and context.job_id:
            self.jobs.invalidate_push_approvals(context.job_id)
            self.jobs.update_context(context.job_id, {"revision_request": text, "push_requested": False})
            self.jobs.transition(context.job_id, JobStatus.REVISION_REQUESTED, {"request": text})
            self.jobs.add_input(context.job_id, text, telegram_user_id=message.user_id, telegram_message_id=message.message_id)
            self.jobs.transition(context.job_id, JobStatus.QUEUED, {"reason": "revision_requested"})
            return await self.send("Registré la revisión y anulé la aprobación anterior. Se volverá a validar el worktree.", chat_id=message.chat_id, job_id=context.job_id, message_type="revision_requested")
        if intent == Intent.APPROVE_PROPOSAL and context and context.job_id:
            proposals = self.jobs.pending_proposals(context.job_id)
            proposal = next((item for item in proposals if item.id.casefold() in text.casefold()), proposals[0] if len(proposals) == 1 else None)
            if not proposal:
                return await self.send("Indica el ID de la propuesta que deseas aprobar.", chat_id=message.chat_id, job_id=context.job_id, message_type="clarification")
            self.jobs.approve_proposal(proposal.id, context.job_id, proposal.base_sha or "", message.user_id, message.message_id)
            self.jobs.transition(context.job_id, JobStatus.PROPOSAL_APPROVED, {"proposal_id": proposal.id, "telegram_user_id": message.user_id})
            project = self.config.project(proposal.project_id)
            repository = self.config.repository(project.repository)
            implementation = self.jobs.create_job(kind="implementation", idempotency_key=f"implementation:{proposal.id}", project_id=project.id, repository_id=repository.id, request_text=proposal.description, context={"proposal_id": proposal.id, "branch_prefix": proposal.category})
            return await self.send(f"Propuesta {proposal.id} aprobada. Creé el job de implementación local; todavía no se hará push.", chat_id=message.chat_id, project_id=project.id, repository_id=repository.id, job_id=implementation.id, message_type="implementation_queued")
        if intent == Intent.REJECT_PROPOSAL and context and context.job_id:
            proposals = self.jobs.pending_proposals(context.job_id)
            proposal = next((item for item in proposals if item.id.casefold() in text.casefold()), proposals[0] if len(proposals) == 1 else None)
            if not proposal:
                return await self.send("Indica el ID de la propuesta que deseas rechazar.", chat_id=message.chat_id, job_id=context.job_id, message_type="clarification")
            self.jobs.reject_proposal(proposal.id, context.job_id)
            self.jobs.transition(context.job_id, JobStatus.PROPOSAL_REJECTED, {"proposal_id": proposal.id, "telegram_user_id": message.user_id})
            return await self.send(f"Propuesta {proposal.id} rechazada; no se modificarán archivos.", chat_id=message.chat_id, job_id=context.job_id, message_type="proposal_rejected")
        if context and context.job_id and state == JobStatus.AWAITING_PUSH_APPROVAL.value:
            return await self.send(
                f"¿Autorizas explícitamente hacer push del HEAD {context.head_sha or 'actual'} y crear el draft PR? Responde `Push it` o `Reject push`.",
                chat_id=message.chat_id,
                job_id=context.job_id,
                message_type="approval_clarification",
                head_sha=context.head_sha,
            )
        if context and context.job_id and state == JobStatus.AWAITING_PROPOSAL_APPROVAL.value:
            return await self.send(
                "Indica explícitamente el ID de la propuesta que deseas aprobar o rechazar.",
                chat_id=message.chat_id,
                job_id=context.job_id,
                message_type="proposal_clarification",
            )
        if intent == Intent.ASK_ABOUT_CHANGE and context and context.job_id:
            self.jobs.add_input(context.job_id, text, telegram_user_id=message.user_id, telegram_message_id=message.message_id)
            question_job = self.jobs.create_job(
                kind="change_question",
                idempotency_key=f"change-question:{context.job_id}:{message.message_id}",
                project_id=context.project_id,
                repository_id=context.repository_id,
                request_text=text,
                context={"target_job_id": context.job_id},
            )
            return await self.send("La pregunta quedó asociada al job y será respondida contra el diff local.", chat_id=message.chat_id, job_id=question_job.id, message_type="question_queued")
        project_id = self._resolve_project(context, classification.project_id, text)
        if intent in {
            Intent.PROJECT_QUESTION,
            Intent.CODE_ANALYSIS,
            Intent.PIPELINE_ANALYSIS,
            Intent.FEATURE_REQUEST,
            Intent.FIX_REQUEST,
            Intent.REFACTOR_REQUEST,
            Intent.TEST_REQUEST,
        } and not project_id:
            return await self.send("¿Qué proyecto lógico debo usar? Responde con su ID configurado.", chat_id=message.chat_id, message_type="clarification")
        if intent == Intent.UNKNOWN:
            return await self.send(
                "No pude identificar una solicitud concreta. Indica qué quieres analizar, cambiar o corregir.",
                chat_id=message.chat_id,
                message_type="clarification",
            )
        if self.job_creator:
            job_id = await self.job_creator(
                intent.value,
                project_id,
                context.repository_id if context else None,
                text,
                message.update_id,
            )
            if self.reactor:
                await self._react_to_message(message, job_id=job_id)
                return None
            return await self.send("Recibido. Creé un job persistente para procesarlo.", chat_id=message.chat_id, job_id=job_id, project_id=project_id, message_type="job_created")
        return await self.send("Recibido, pero el worker todavía no está disponible.", chat_id=message.chat_id, message_type="status")

    async def _react_to_message(self, message: InboundMessage, *, job_id: str | None = None) -> None:
        if message.acknowledged or not self.reactor:
            return
        try:
            await self.reactor(message.chat_id, message.message_id, ACK_REACTION)
        except Exception:
            logger.exception(
                "Could not react to Telegram message",
                extra={"event_type": "TELEGRAM_REACTION_FAILURE", "job_id": job_id},
            )

    async def acknowledge(self, message: InboundMessage) -> None:
        """Send the immediate Telegram receipt before any expensive processing."""
        if self.authorized(message.user_id, message.chat_id):
            await self._react_to_message(message)

    async def handle_callback(self, callback_id: str, data: str, *, user_id: str, chat_id: str, message_id: str) -> str | None:
        if data.startswith("confirm_job:"):
            return await self.handle(InboundMessage(callback_id, chat_id, user_id, str(uuid.uuid4()), "Confirm", message_id))
        if data.startswith("cancel_job:"):
            return await self.handle(InboundMessage(callback_id, chat_id, user_id, str(uuid.uuid4()), "Cancel", message_id))
        if data == "approve_push":
            result = await self.handle(
                InboundMessage(
                    callback_id,
                    chat_id,
                    user_id,
                    str(uuid.uuid4()),
                    "Push it",
                    message_id,
                    intent_override=Intent.APPROVE_PUSH,
                )
            )
            return result
        if data == "reject_push":
            return await self.handle(
                InboundMessage(
                    callback_id,
                    chat_id,
                    user_id,
                    str(uuid.uuid4()),
                    "Reject push",
                    message_id,
                    intent_override=Intent.REJECT_PUSH,
                )
            )
        return await self.send("Acción de callback no reconocida.", chat_id=chat_id, message_type="clarification")

    async def transcribe_voice(self, voice_file_id: str) -> str:
        raise RuntimeError(f"Voice file download must be supplied by the Telegram adapter: {voice_file_id}")

    async def send(
        self,
        text: str,
        *,
        chat_id: str,
        message_type: str,
        project_id: str | None = None,
        repository_id: str | None = None,
        job_id: str | None = None,
        interaction_id: str | None = None,
        thread_id: str | None = None,
        execution_phase: str | None = None,
        branch_name: str | None = None,
        head_sha: str | None = None,
        parse_mode: str | None = None,
        reply_markup: ReplyMarkup | None = None,
        **_extra: Any,
    ) -> str:
        effective_markup = reply_markup
        if message_type == "push_approval" and effective_markup is None:
            effective_markup = [[("Aprobar push", "approve_push"), ("Rechazar push", "reject_push")]]
        if self.sender_with_options:
            message_id = await self.sender_with_options(chat_id, text, parse_mode, effective_markup)
        elif self.sender:
            message_id = await self.sender(chat_id, text)
        else:
            message_id = str(uuid.uuid4())
        with self.database.session() as session:
            if thread_id:
                thread = session.get(ConversationThread, thread_id)
                if thread:
                    thread.updated_at = utcnow()
                    thread.job_id = job_id or thread.job_id
                    thread.project_id = project_id or thread.project_id
                    thread.repository_id = repository_id or thread.repository_id
                else:
                    session.add(ConversationThread(id=thread_id, chat_id=chat_id, project_id=project_id, repository_id=repository_id, job_id=job_id))
            session.add(
                TelegramOutboundMessage(
                    chat_id=chat_id,
                    message_id=message_id,
                    message_type=message_type,
                    text=text,
                    project_id=project_id,
                    repository_id=repository_id,
                    job_id=job_id,
                    interaction_id=interaction_id,
                    thread_id=thread_id,
                    execution_phase=execution_phase,
                    branch_name=branch_name,
                    head_sha=head_sha,
                )
            )
            session.commit()
        return message_id

    def _persist_inbound(self, message: InboundMessage, text: str) -> None:
        with self.database.session() as session:
            existing = session.scalar(select(TelegramInboundMessage).where(TelegramInboundMessage.update_id == message.update_id))
            if existing:
                return
            session.add(
                TelegramInboundMessage(
                    update_id=message.update_id,
                    chat_id=message.chat_id,
                    user_id=message.user_id,
                    message_id=message.message_id,
                    reply_to_message_id=message.reply_to_message_id,
                    text=message.text,
                    voice_file_id=message.voice_file_id,
                    transcript=text if message.voice_file_id else None,
                )
            )
            session.commit()

    def _set_intent(self, update_id: str, intent: str) -> None:
        with self.database.session() as session:
            item = session.scalar(select(TelegramInboundMessage).where(TelegramInboundMessage.update_id == update_id))
            if item:
                item.intent = intent
                session.commit()

    def _find_project(self, text: str) -> str | None:
        for project in self.config.projects:
            if re.search(rf"\b{re.escape(project.id)}\b", text, re.IGNORECASE):
                return project.id
        return None

    def _resolve_project(self, context: ReplyContext | None, suggested: str | None, text: str) -> str | None:
        if context and context.project_id:
            return context.project_id
        if suggested:
            for project in self.config.projects:
                if project.id.casefold() == suggested.casefold():
                    return project.id
        return self._find_project(text)

    @staticmethod
    def _is_pending_input_response(text: str) -> bool:
        return bool(
            re.search(
                r"\b(confirm|confirmed|confirmar|confirmo|cancel|cancelar|cancelo|yes|si|s\u00ed|ok|okay|dale|adelante|exacto|rechazo|no)\b",
                text.casefold(),
            )
        )

    def build_application(self) -> Any:
        """Build the optional python-telegram-bot application for long polling."""
        try:
            from telegram.ext import ApplicationBuilder
        except ImportError as exc:
            raise RuntimeError("Install orchestrator[telegram] to use Telegram long polling") from exc
        token = os.getenv(self.config.telegram.bot_token_env)
        if not token:
            raise RuntimeError(f"Telegram bot token is not configured: {self.config.telegram.bot_token_env}")
        return ApplicationBuilder().token(token).build()


def _csv_env(name: str) -> set[str]:
    value = os.getenv(name, "")
    return {item.strip() for item in value.split(",") if item.strip()}
