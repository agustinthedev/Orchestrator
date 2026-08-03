from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


class Repository(Base):
    __tablename__ = "repositories"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255))
    local_path: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(40), default="none")
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    remote_name: Mapped[str] = mapped_column(String(100), default="origin")
    provider_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255))
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id"), index=True)
    working_directory: Mapped[str] = mapped_column(String(1000), default=".")
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    codex_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pipelines: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    workflow: Mapped[str] = mapped_column(String(80))
    cron: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    misfire_grace_time: Mapped[int] = mapped_column(Integer, default=300)
    coalesce: Mapped[bool] = mapped_column(Boolean, default=True)
    max_instances: Mapped[int] = mapped_column(Integer, default=1)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    repository_id: Mapped[str | None] = mapped_column(ForeignKey("repositories.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(60), index=True)
    phase: Mapped[str | None] = mapped_column(String(80), nullable=True)
    request_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobInput(Base):
    __tablename__ = "job_inputs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    source: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    telegram_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    telegram_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobQuestion(Base):
    __tablename__ = "job_questions"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(120))
    question: Mapped[str] = mapped_column(Text)
    question_type: Mapped[str] = mapped_column(String(40))
    options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobAnswer(Base):
    __tablename__ = "job_answers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("job_questions.id"), index=True)
    answer: Mapped[Any] = mapped_column(JSON)
    telegram_user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id"), index=True)
    review_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    category: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    affected_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    scope_estimate: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    risk: Mapped[str | None] = mapped_column(String(40), nullable=True)
    suggested_validation: Mapped[list[str]] = mapped_column(JSON, default=list)
    likely_migration: Mapped[bool] = mapped_column(Boolean, default=False)
    shared_code_affected: Mapped[bool] = mapped_column(Boolean, default=False)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProposalApproval(Base):
    __tablename__ = "proposal_approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    base_sha: Mapped[str] = mapped_column(String(64))
    telegram_user_id: Mapped[str] = mapped_column(String(100))
    telegram_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PushApproval(Base):
    __tablename__ = "push_approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    approved_head_sha: Mapped[str] = mapped_column(String(64))
    approved_by_telegram_user_id: Mapped[str] = mapped_column(String(100))
    approval_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CodexExecution(Base):
    __tablename__ = "codex_executions"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    phase: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(255))
    reasoning_effort: Mapped[str] = mapped_column(String(50))
    mode: Mapped[str] = mapped_column(String(40))
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    command: Mapped[list[str]] = mapped_column(JSON, default=list)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Worktree(Base):
    __tablename__ = "worktrees"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    branch_name: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Branch(Base):
    __tablename__ = "branches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    base_branch: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Commit(Base):
    __tablename__ = "commits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    sha: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(500))
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FileChange(Base):
    __tablename__ = "file_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(String(20))
    additions: Mapped[int] = mapped_column(Integer, default=0)
    deletions: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ValidationRun(Base):
    __tablename__ = "validation_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    command: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_summary: Mapped[str] = mapped_column(Text, default="")
    stderr_summary: Mapped[str] = mapped_column(Text, default="")
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PullRequest(Base):
    __tablename__ = "pull_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(40))
    provider_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(30), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40))
    failure_class: Mapped[str | None] = mapped_column(String(80), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TelegramInboundMessage(Base):
    __tablename__ = "telegram_inbound_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    update_id: Mapped[str] = mapped_column(String(120), unique=True)
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    message_id: Mapped[str] = mapped_column(String(100))
    reply_to_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent: Mapped[str | None] = mapped_column(String(60), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TelegramOutboundMessage(Base):
    __tablename__ = "telegram_outbound_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    message_id: Mapped[str] = mapped_column(String(100), index=True)
    message_type: Mapped[str] = mapped_column(String(80))
    text: Mapped[str] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    repository_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    interaction_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    execution_phase: Mapped[str | None] = mapped_column(String(80), nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ConversationThread(Base):
    __tablename__ = "conversation_threads"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    project_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    repository_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(50), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CredentialsMetadata(Base):
    __tablename__ = "credentials_metadata"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    environment_variable: Mapped[str] = mapped_column(String(120))
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


Index("ix_proposal_job_status", Proposal.job_id, Proposal.status)
Index("ix_push_approval_job_status", PushApproval.job_id, PushApproval.status)
UniqueConstraint(ProposalApproval.proposal_id, ProposalApproval.telegram_user_id, name="uq_proposal_user")

