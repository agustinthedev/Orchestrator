from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApplicationSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = "orchestrator"
    environment: str = "development"
    timezone: str = "America/Montevideo"


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str = "sqlite:///data/orchestrator.db"


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    worktrees_root: Path = Path("data/worktrees")
    logs_root: Path = Path("logs")
    temporary_root: Path = Path("data/tmp")
    approvals_ttl_minutes: int = Field(default=60, ge=1, le=7 * 24 * 60)


class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    max_concurrent_codex_runs: int = Field(default=2, ge=1)
    max_write_jobs_per_repository: int = Field(default=1, ge=1)
    max_read_jobs_per_repository: int = Field(default=3, ge=1)
    poll_interval_seconds: float = Field(default=2, ge=0.2)


class TranscriptionSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider: Literal["openai", "none"] = "openai"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = "configurable-transcription-model"
    confirm_read_only_transcriptions: bool = False
    confirm_write_transcriptions: bool = True


class TelegramSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    allowed_user_ids_env: str = "TELEGRAM_ALLOWED_USER_IDS"
    allowed_chat_ids_env: str = "TELEGRAM_ALLOWED_CHAT_IDS"
    conversation_chat_id_env: str = "TELEGRAM_CONVERSATION_CHAT_ID"
    status_chat_id_env: str = "TELEGRAM_STATUS_CHAT_ID"
    transcription: TranscriptionSettings = Field(default_factory=TranscriptionSettings)
    notification_routes: dict[str, list[str]] = Field(default_factory=dict)


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = "configurable-luna-model-name"
    reasoning_effort: str = "extra_high"


class CodexSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    executable: str = "codex"
    default_model: ModelSpec = Field(default_factory=ModelSpec)
    model_routing_enabled: bool = True
    allowed_models: list[str] = Field(default_factory=lambda: ["configurable-luna-model-name"])
    allowed_reasoning_efforts: list[str] = Field(
        default_factory=lambda: ["low", "medium", "high", "extra_high"]
    )
    extra_args: list[str] = Field(default_factory=list)


class GitSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    allow_force_push: bool = False
    allow_direct_default_branch_push: bool = False
    require_push_approval: bool = True
    verify_approved_head_sha: bool = True


class PullRequestSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    always_draft: bool = True
    allow_auto_merge: bool = False
    allow_auto_complete: bool = False

    @field_validator("always_draft")
    @classmethod
    def enforce_draft(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Pull requests must always be drafts")
        return value


class RemoteSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = "origin"


class GitHubSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    owner: str
    repository: str
    token_env: str = "GITHUB_TOKEN"


class AzureDevOpsSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    organization: str
    project: str
    repository_id: str
    pat_env: str = "AZURE_DEVOPS_PAT"


class RepositoryConcurrency(BaseModel):
    max_write_jobs: int = Field(default=1, ge=1)
    max_read_jobs: int = Field(default=3, ge=1)


class RepositoryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1)
    display_name: str
    local_path: Path
    provider: Literal["github", "azure_devops", "none"] = "none"
    default_branch: str = "main"
    remote: RemoteSettings = Field(default_factory=RemoteSettings)
    github: GitHubSettings | None = None
    azure_devops: AzureDevOpsSettings | None = None
    concurrency: RepositoryConcurrency = Field(default_factory=RepositoryConcurrency)


class ScopeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    allowed_read_paths: list[str] = Field(default_factory=lambda: ["."])
    allowed_write_paths: list[str] = Field(default_factory=lambda: ["."])
    forbidden_paths: list[str] = Field(default_factory=list)


class ValidationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    commands: list[str] = Field(default_factory=list)
    required: bool = True


class ProjectCodexConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    profile_path: Path | None = None
    model_override: str | None = None
    reasoning_effort_override: str | None = None
    enabled: bool = True


class ProjectPermissions(BaseModel):
    model_config = ConfigDict(extra="ignore")
    allow_read: bool = True
    allow_changes: bool = True
    allow_push: bool = True
    allow_pull_request: bool = True
    allow_merge: bool = False

    @field_validator("allow_merge")
    @classmethod
    def no_merge(cls, value: bool) -> bool:
        if value:
            raise ValueError("Automatic merge is not supported")
        return value


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    provider: Literal["github", "azure_devops"]
    pipeline_id: str | int


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1)
    display_name: str
    repository: str
    working_directory: str = "."
    codex: ProjectCodexConfig = Field(default_factory=ProjectCodexConfig)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    pipelines: dict[str, PipelineConfig] = Field(default_factory=dict)
    permissions: ProjectPermissions = Field(default_factory=ProjectPermissions)


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1)
    project: str
    workflow: Literal["daily_code_review", "pipeline_review", "global_question", "project_question"]
    cron: str
    enabled: bool = True
    timezone: str | None = None
    misfire_grace_time: int = Field(default=300, ge=1)
    coalesce: bool = True
    max_instances: int = Field(default=1, ge=1)
    parameters: dict[str, object] = Field(default_factory=dict)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    application: ApplicationSettings = Field(default_factory=ApplicationSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    workers: WorkerSettings = Field(default_factory=WorkerSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    pull_requests: PullRequestSettings = Field(default_factory=PullRequestSettings)
    repositories: list[RepositoryConfig] = Field(default_factory=list)
    projects: list[ProjectConfig] = Field(default_factory=list)
    schedules: list[ScheduleConfig] = Field(default_factory=list)
    config_dir: Path = Path("config")

    def repository(self, repository_id: str) -> RepositoryConfig:
        for repository in self.repositories:
            if repository.id == repository_id:
                return repository
        raise KeyError(f"Unknown repository: {repository_id}")

    def project(self, project_id: str) -> ProjectConfig:
        for project in self.projects:
            if project.id == project_id:
                return project
        raise KeyError(f"Unknown project: {project_id}")

    def resolve_project_path(self, project_id: str, root: Path | None = None) -> Path:
        project = self.project(project_id)
        repository = self.repository(project.repository)
        base = root or repository.local_path
        resolved = (base / project.working_directory).resolve()
        if not is_within(resolved, base.resolve()):
            raise ValueError(f"Project working directory escapes repository: {project.working_directory}")
        return resolved


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
