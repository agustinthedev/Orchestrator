from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from orchestrator.codex.runner import CodexRunner
from orchestrator.config import load_config
from orchestrator.config.loader import configured_secret_names
from orchestrator.config.models import AppConfig
from orchestrator.database.engine import Database, create_database
from orchestrator.database.models import CredentialsMetadata, Project, Repository, utcnow
from orchestrator.git.manager import GitManager
from orchestrator.jobs.service import JobService
from orchestrator.observability.logging import configure_logging, get_logger
from orchestrator.scheduling.scheduler import SchedulerService
from orchestrator.telegram.gateway import InboundMessage, TelegramGateway
from orchestrator.transcription.adapters import NullTranscriber, OpenAITranscriber
from orchestrator.validation import ValidationRunner
from orchestrator.workers.worker import WorkerPool
from orchestrator.workflows.engine import OrchestratorEngine

logger = get_logger(__name__)


class Application:
    def __init__(self, config: AppConfig) -> None:
        configure_logging(config.runtime.logs_root)
        self.config = config
        self.database: Database = create_database(config.database.url)
        self._persist_config()
        self._persist_credential_metadata()
        self.jobs = JobService(self.database, approval_ttl_minutes=config.runtime.approvals_ttl_minutes)
        self.codex = CodexRunner(config.codex)
        self.git = GitManager(config.git, config.runtime.worktrees_root)
        self.validator = ValidationRunner()
        transcriber = (
            OpenAITranscriber(config.telegram.transcription.api_key_env, config.telegram.transcription.model)
            if config.telegram.transcription.provider == "openai"
            else NullTranscriber()
        )
        self.telegram = TelegramGateway(config, self.database, self.jobs, transcriber=transcriber, job_creator=self._create_message_job)
        self.engine = OrchestratorEngine(config, self.database, self.jobs, self.codex, self.git, self.validator, notifier=self.telegram)
        self.workers = WorkerPool(self.jobs, self.engine.handle_job, count=config.workers.max_concurrent_codex_runs, poll_interval=config.workers.poll_interval_seconds)
        self.scheduler = SchedulerService(config, self.database, self.jobs, self._scheduled_workflow)
        self._telegram_app: Any = None

    def _persist_config(self) -> None:
        with self.database.session() as session:
            for repository_config in self.config.repositories:
                repository_record = session.get(Repository, repository_config.id)
                repository_values = {"display_name": repository_config.display_name, "local_path": str(repository_config.local_path), "provider": repository_config.provider, "default_branch": repository_config.default_branch, "remote_name": repository_config.remote.name, "provider_config": repository_config.model_dump(exclude={"id", "display_name", "local_path", "provider", "default_branch", "remote", "concurrency"})}
                if repository_record:
                    for key, value in repository_values.items():
                        setattr(repository_record, key, value)
                else:
                    session.add(Repository(id=repository_config.id, **repository_values))
            session.flush()
            for project_config in self.config.projects:
                project_repository = self.config.repository(project_config.repository)
                project_record = session.get(Project, project_config.id)
                project_values = {"display_name": project_config.display_name, "repository_id": project_repository.id, "working_directory": project_config.working_directory, "scope": project_config.scope.model_dump(mode="json"), "codex_config": project_config.codex.model_dump(mode="json"), "validation_config": project_config.validation.model_dump(mode="json"), "permissions": project_config.permissions.model_dump(mode="json"), "pipelines": {key: value.model_dump(mode="json") for key, value in project_config.pipelines.items()}}
                if project_record:
                    for key, value in project_values.items():
                        setattr(project_record, key, value)
                else:
                    session.add(Project(id=project_config.id, **project_values))
            session.commit()

    def _persist_credential_metadata(self) -> None:
        with self.database.session() as session:
            for name in configured_secret_names(self.config):
                existing = session.query(CredentialsMetadata).filter(CredentialsMetadata.name == name).one_or_none()
                if existing:
                    existing.available = bool(os.getenv(name))
                    existing.checked_at = utcnow()
                else:
                    session.add(CredentialsMetadata(name=name, environment_variable=name, available=bool(os.getenv(name))))
            session.commit()

    async def _create_message_job(
        self,
        intent: str,
        project_id: str | None,
        repository_id: str | None,
        text: str,
        update_id: str,
    ) -> str:
        kind = {
            "GLOBAL_QUESTION": "global_question",
            "PROJECT_QUESTION": "project_question",
            "PIPELINE_ANALYSIS": "pipeline_review",
            "CODE_ANALYSIS": "daily_code_review",
        }.get(intent, intent)
        digest = hashlib.sha256(f"{update_id}\0{intent}\0{project_id or ''}\0{text}".encode()).hexdigest()
        job = self.jobs.create_job(kind=kind, idempotency_key=f"telegram:{digest}", project_id=project_id, repository_id=repository_id, request_text=text, context={})
        return job.id

    async def _scheduled_workflow(self, _schedule: Any) -> None:
        return None

    async def start(self) -> None:
        recovered = self.jobs.recover()
        logger.info("Orchestrator started", extra={"event_type": "SERVICE_STARTED"})
        if recovered:
            logger.info("Recovered jobs", extra={"event_type": "RECOVERY", "correlation_id": str(recovered)})
        self.scheduler.start()
        await self.workers.start()
        await self._start_telegram()
        await self.telegram.send("Orchestrator iniciado.", chat_id=os.getenv(self.config.telegram.status_chat_id_env, "status"), message_type="service_start")

    async def stop(self) -> None:
        await self.workers.stop()
        self.scheduler.shutdown()
        if self._telegram_app:
            await self._telegram_app.updater.stop()
            await self._telegram_app.stop()
            await self._telegram_app.shutdown()
        await self.telegram.send("Orchestrator detenido.", chat_id=os.getenv(self.config.telegram.status_chat_id_env, "status"), message_type="service_stop")
        self.database.dispose()

    async def run_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    async def _start_telegram(self) -> None:
        if not os.getenv(self.config.telegram.bot_token_env):
            logger.warning("Telegram disabled because bot token is absent", extra={"event_type": "CREDENTIAL_MISSING"})
            return
        self._telegram_app = self.telegram.build_application()

        async def sender(chat_id: str, text: str) -> str:
            message = await self._telegram_app.bot.send_message(chat_id=chat_id, text=text)
            return str(message.message_id)

        async def reactor(chat_id: str, message_id: str, emoji: str) -> None:
            await self._telegram_app.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=int(message_id),
                reaction=emoji,
            )

        self.telegram.sender = sender
        self.telegram.reactor = reactor
        try:
            from telegram.ext import CallbackQueryHandler, MessageHandler, filters
        except ImportError as exc:
            raise RuntimeError("Install orchestrator[telegram] for Telegram support") from exc

        async def handle_update(update: Any, _context: Any) -> None:
            message = update.effective_message
            if not message or not update.effective_user or not update.effective_chat:
                return
            text = message.text
            voice_file_id = None
            if message.voice:
                voice_file_id = str(message.voice.file_id)
                file = await self._telegram_app.bot.get_file(message.voice.file_id)
                temporary = self.config.runtime.temporary_root
                temporary.mkdir(parents=True, exist_ok=True)
                audio_path = temporary / f"voice-{message.voice.file_unique_id}.ogg"
                await file.download_to_drive(custom_path=str(audio_path))
                text = await self.telegram.transcriber.transcribe(audio_path)
            await self.telegram.handle(InboundMessage(update_id=str(update.update_id), chat_id=str(update.effective_chat.id), user_id=str(update.effective_user.id), message_id=str(message.message_id), text=text, reply_to_message_id=str(message.reply_to_message.message_id) if message.reply_to_message else None, voice_file_id=voice_file_id))

        self._telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_update))
        self._telegram_app.add_handler(MessageHandler(filters.VOICE, handle_update))

        async def handle_callback(update: Any, _context: Any) -> None:
            query = update.callback_query
            if not query or not query.message or not query.from_user or not query.message.chat:
                return
            await query.answer()
            await self.telegram.handle_callback(
                str(query.id),
                str(query.data),
                user_id=str(query.from_user.id),
                chat_id=str(query.message.chat.id),
                message_id=str(query.message.message_id),
            )

        self._telegram_app.add_handler(CallbackQueryHandler(handle_callback))
        await self._telegram_app.initialize()
        await self._telegram_app.start()
        if self._telegram_app.updater:
            await self._telegram_app.updater.start_polling()
        self.telegram.connected = True


def build_application(config_dir: Path | str | None = None) -> Application:
    load_dotenv()
    directory: Path | str = config_dir if config_dir is not None else os.getenv("ORCHESTRATOR_CONFIG_DIR", "config")
    return Application(load_config(directory))


def main() -> None:
    asyncio.run(build_application().run_forever())
