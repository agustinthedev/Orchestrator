from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select

from orchestrator.codex.runner import CodexRunner
from orchestrator.config.models import AppConfig, ProjectConfig, RepositoryConfig
from orchestrator.database.engine import Database
from orchestrator.database.models import (
    CodexExecution,
    Commit,
    FileChange,
    Job,
    PipelineRun,
    Proposal,
    PullRequest,
    ValidationRun,
    Worktree,
    utcnow,
)
from orchestrator.domain import JobStatus
from orchestrator.git.manager import GitManager
from orchestrator.jobs.service import JobService
from orchestrator.observability.logging import get_logger
from orchestrator.pipelines.analyzer import PipelineAnalyzer
from orchestrator.providers.azure_devops import AzureDevOpsProvider
from orchestrator.providers.base import SourceControlProvider, draft_pr_description
from orchestrator.providers.github import GitHubProvider
from orchestrator.validation import ValidationRunner

logger = get_logger(__name__)


class Notifier(Protocol):
    async def send(self, text: str, *, chat_id: str, message_type: str, **kwargs: Any) -> str: ...


class NullNotifier:
    async def send(self, text: str, *, chat_id: str, message_type: str, **kwargs: Any) -> str:
        logger.info(text, extra={"event_type": kwargs.get("message_type", "NOTIFICATION")})
        return str(uuid.uuid4())


class OrchestratorEngine:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        jobs: JobService,
        codex: CodexRunner,
        git: GitManager,
        validator: ValidationRunner,
        *,
        notifier: Notifier | None = None,
        prompts_root: Path = Path("prompts"),
    ) -> None:
        self.config = config
        self.database = database
        self.jobs = jobs
        self.codex = codex
        self.git = git
        self.validator = validator
        self.notifier = notifier or NullNotifier()
        self.prompts_root = prompts_root

    async def handle_job(self, job: Job) -> None:
        if job.context.get("push_requested"):
            await self.push(job)
            return
        if job.kind in {"daily_code_review", "CODE_ANALYSIS"}:
            await self.daily_code_review(job)
        elif job.kind in {"pipeline_review", "PIPELINE_ANALYSIS"}:
            await self.pipeline_review(job)
        elif job.kind in {"global_question", "GLOBAL_QUESTION"}:
            await self.question(job, global_scope=True)
        elif job.kind in {"project_question", "PROJECT_QUESTION"}:
            await self.question(job, global_scope=False)
        elif job.kind in {"implementation", "FEATURE_REQUEST", "FIX_REQUEST", "REFACTOR_REQUEST", "TEST_REQUEST"}:
            await self.implementation(job)
        elif job.kind in {"change_question", "ASK_ABOUT_CHANGE"}:
            await self.change_question(job)
        else:
            raise ValueError(f"Unsupported job kind: {job.kind}")

    async def daily_code_review(self, job: Job) -> None:
        project, repository = self._project_repository(job)
        self._ensure_read_allowed(project)
        base_sha = self.git.base_sha(repository)
        prompt = self._prompt("daily_code_review", self._scope_context(project, repository, job, base_sha))
        result = await self.codex.run(
            cwd=self.config.resolve_project_path(project.id),
            prompt=prompt,
            model=self.codex.choose_model(project, task="daily_code_review"),
            mode="read_only",
            profile_path=project.codex.profile_path,
        )
        self._record_codex(job, result, "daily_code_review")
        if await self._request_input_if_needed(job, result):
            return
        if result.exit_code != 0 or not result.structured:
            raise RuntimeError(f"Code review Codex run failed: {result.stderr[-1000:]}")
        created = self._persist_proposals(job, project, repository, base_sha, result.structured.proposals)
        if created:
            self.jobs.transition(job.id, JobStatus.AWAITING_PROPOSAL_APPROVAL, {"proposal_count": len(created)})
            await self.notifier.send(
                self._proposal_summary(project, created),
                chat_id=self._conversation_chat_id(),
                message_type="proposal_summary",
                project_id=project.id,
                repository_id=repository.id,
                job_id=job.id,
                phase="awaiting_proposal_approval",
                thread_id=f"THREAD-{job.id}-PROPOSALS",
            )
        else:
            self.jobs.transition(job.id, JobStatus.COMPLETED, {"proposal_count": 0})

    async def pipeline_review(self, job: Job) -> None:
        project, repository = self._project_repository(job)
        pipeline_name = str(job.context.get("parameters", {}).get("pipeline", ""))
        pipeline = project.pipelines.get(pipeline_name)
        if not pipeline:
            raise ValueError(f"No pipeline '{pipeline_name}' configured for project {project.id}")
        provider = self.provider_for(repository)
        if not provider:
            raise ValueError(f"No source-control provider configured for repository {repository.id}")
        diagnostic = PipelineAnalyzer(provider).latest_diagnostic(str(pipeline.pipeline_id))
        if not diagnostic:
            self.jobs.transition(job.id, JobStatus.COMPLETED, {"message": "No relevant pipeline run"})
            return
        run = diagnostic["run"]
        with self.database.session() as session:
            session.add(
                PipelineRun(
                    job_id=job.id,
                    provider=repository.provider,
                    external_id=run.external_id,
                    status=run.status,
                    failure_class=diagnostic.get("failure_class"),
                    summary="\n".join(diagnostic.get("evidence", [])),
                    raw_metadata=run.metadata,
                )
            )
            session.commit()
        if diagnostic.get("successful"):
            self.jobs.transition(job.id, JobStatus.COMPLETED, {"pipeline": "successful"})
            return
        message = f"Pipeline failure for {project.display_name}\nClass: {diagnostic.get('failure_class')}\nEvidence:\n" + "\n".join(
            f"- {item}" for item in diagnostic.get("evidence", [])
        )
        await self.notifier.send(message, chat_id=self._conversation_chat_id(), message_type="pipeline_failure", project_id=project.id, repository_id=repository.id, job_id=job.id)
        self.jobs.transition(job.id, JobStatus.COMPLETED, {"fix_proposal_supported": diagnostic.get("code_fix_supported", False)})

    async def question(self, job: Job, *, global_scope: bool) -> None:
        project = None if global_scope else self.config.project(job.project_id or "")
        repository = self.config.repository(project.repository) if project else None
        if project:
            cwd = self.config.resolve_project_path(project.id)
            profile = project.codex.profile_path
            model = self.codex.choose_model(project, task="project_question")
            context = self._scope_context(project, repository, job, None)
        else:
            cwd = Path.cwd()
            profile = None
            model = self.codex.choose_model(None, task="global_question")
            context = f"Orchestrator root: {cwd}\nSafe metadata only.\nQuestion: {job.request_text}"
        prompt = self._prompt("global_question" if global_scope else "project_question", context)
        result = await self.codex.run(cwd=cwd, prompt=prompt, model=model, mode="read_only", profile_path=profile)
        self._record_codex(job, result, "global_question" if global_scope else "project_question")
        if await self._request_input_if_needed(job, result):
            return
        if result.exit_code != 0:
            raise RuntimeError(result.stderr[-1000:])
        answer = result.structured.answer if result.structured else result.stdout[-4000:]
        await self.notifier.send(answer or "No se obtuvo una respuesta estructurada.", chat_id=self._conversation_chat_id(), message_type="answer", project_id=project.id if project else None, job_id=job.id)
        self.jobs.transition(job.id, JobStatus.COMPLETED, {})

    async def implementation(self, job: Job) -> None:
        project, repository = self._project_repository(job)
        self._ensure_write_allowed(project)
        if not await self._revalidate_stale_proposal(job, project, repository):
            return
        target_job_id = str(job.context.get("target_job_id", ""))
        worktree: Any = self._worktree_for(target_job_id or job.id)
        if not worktree:
            self.jobs.transition(job.id, JobStatus.PREPARING_WORKTREE, {})
            proposal_id = str(job.context.get("proposal_id", ""))
            info = self.git.create_worktree(repository, job_id=job.id, proposal_id=proposal_id, prefix=str(job.context.get("branch_prefix", "fix")))
            with self.database.session() as session:
                session.add(Worktree(id=info.id, job_id=job.id, repository_id=repository.id, path=str(info.path), branch_name=info.branch, base_sha=info.base_sha))
                current = session.get(Job, job.id)
                if current:
                    current.worktree_path = str(info.path)
                    current.branch_name = info.branch
                    current.base_sha = info.base_sha
                    current.updated_at = utcnow()
                session.commit()
            worktree = info
        worktree_path = Path(worktree.path)
        self.jobs.transition(job.id, JobStatus.IMPLEMENTING_REVISION if job.context.get("revision_request") else JobStatus.IMPLEMENTING, {})
        prompt = self._prompt("revision" if job.context.get("revision_request") else "implementation", self._scope_context(project, repository, job, worktree.base_sha) + f"\nWorktree: {worktree_path}\nRevision: {job.context.get('revision_request', '')}")
        result = await self.codex.run(cwd=self.config.resolve_project_path(project.id, worktree_path), prompt=prompt, model=self.codex.choose_model(project, task="implementation"), mode="workspace_write", profile_path=project.codex.profile_path)
        self._record_codex(job, result, "implementation")
        if await self._request_input_if_needed(job, result):
            return
        if result.exit_code != 0:
            raise RuntimeError(result.stderr[-1500:])
        allowed, violations = self.git.enforce_scope(worktree_path, worktree.base_sha, project.scope)
        if not allowed:
            self.jobs.transition(job.id, JobStatus.NEEDS_REVIEW, {"forbidden_paths": violations})
            await self.notifier.send(f"Trabajo detenido: Codex modificó rutas fuera de scope: {', '.join(violations)}", chat_id=self._status_chat_id(), message_type="needs_review", project_id=project.id, job_id=job.id)
            return
        validation_results = await self._validate(job, project, worktree_path)
        if project.validation.required and any(not item.passed and not item.skipped for item in validation_results):
            raise RuntimeError("Required validation failed; push approval was not requested")
        head = self.git.current_head(worktree_path)
        branch_name = worktree.branch_name if hasattr(worktree, "branch_name") else worktree.branch
        commits = self.git.commits_since(worktree_path, worktree.base_sha)
        changes = self.git.changed_files(worktree_path, worktree.base_sha)
        with self.database.session() as session:
            current = session.get(Job, job.id)
            if current:
                current.head_sha = head
            for sha, subject in commits:
                session.add(Commit(job_id=job.id, sha=sha, subject=subject))
            for change in changes:
                session.add(FileChange(job_id=job.id, path=change.path, change_type=change.change_type, additions=change.additions, deletions=change.deletions, summary=change.summary))
            session.commit()
        self.jobs.create_push_approval(job.id, head, "system-pending")
        self.jobs.transition(job.id, JobStatus.AWAITING_PUSH_APPROVAL, {"head_sha": head, "files": len(changes)})
        await self.notifier.send(self._push_manifest(project, repository, job, worktree, commits, changes, validation_results), chat_id=self._conversation_chat_id(), message_type="push_approval", project_id=project.id, repository_id=repository.id, job_id=job.id, phase="awaiting_push_approval", branch_name=branch_name, head_sha=head, thread_id=f"THREAD-{job.id}-PUSH")

    async def change_question(self, job: Job) -> None:
        target_id = str(job.context.get("target_job_id", ""))
        target = self.jobs.get(target_id)
        if not target:
            raise ValueError("Target change job no longer exists")
        project, repository = self._project_repository(target)
        worktree = self._worktree_for(target.id)
        if not worktree:
            raise ValueError("Target change job has no worktree")
        worktree_path = Path(worktree.path)
        diff = self.git.run(worktree_path, "diff", "--stat", f"{worktree.base_sha}..HEAD").stdout
        context = self._scope_context(project, repository, target, worktree.base_sha) + f"\nLocal diff stat:\n{diff}\nQuestion: {job.request_text}"
        prompt = self._prompt("answer_change_question", context)
        result = await self.codex.run(
            cwd=self.config.resolve_project_path(project.id, worktree_path),
            prompt=prompt,
            model=self.codex.choose_model(project, task="diff_summarization"),
            mode="read_only",
            profile_path=project.codex.profile_path,
        )
        self._record_codex(job, result, "diff_explanation")
        if result.exit_code != 0:
            raise RuntimeError(result.stderr[-1000:])
        answer = result.structured.answer if result.structured else result.stdout[-4000:]
        await self.notifier.send(
            answer or "No se obtuvo una explicación estructurada.",
            chat_id=self._conversation_chat_id(),
            message_type="diff_explanation",
            project_id=project.id,
            repository_id=repository.id,
            job_id=job.id,
            branch_name=worktree.branch_name,
            head_sha=self.git.current_head(worktree_path),
        )
        self.jobs.transition(job.id, JobStatus.COMPLETED, {})

    async def push(self, job: Job) -> None:
        project, repository = self._project_repository(job)
        target_job_id = str(job.context.get("target_job_id", ""))
        worktree = self._worktree_for(target_job_id or job.id)
        if not worktree:
            raise RuntimeError("No worktree associated with push job")
        approval = self._approved_push(job.id)
        if not approval:
            raise RuntimeError("No approved push record")
        self.jobs.transition(job.id, JobStatus.PUSHING, {"head_sha": approval.approved_head_sha})
        pushed_head = self.git.push_after_approval(repository, Path(worktree.path), branch=worktree.branch_name, default_branch=repository.default_branch, approved_head_sha=approval.approved_head_sha)
        self.jobs.transition(job.id, JobStatus.CREATING_DRAFT_PR, {"head_sha": pushed_head})
        provider = self.provider_for(repository)
        if not provider or not project.permissions.allow_pull_request:
            raise RuntimeError("Pull-request provider or project permission is not configured")
        body = draft_pr_description(summary=f"Resolves `{job.id}`.", problem=job.request_text or "Approved Orchestrator change.", changes=["See the local commit and file manifest."], validation=["See persisted validation runs."], risk="Review required.", limitations=["Draft PR; no automatic merge or completion."], files=[], proposal_id=job.context.get("proposal_id"), commits=[], base_sha=worktree.base_sha, head_sha=pushed_head)
        pr = provider.create_draft_pull_request(title=f"Orchestrator: {job.id}", body=body, head=worktree.branch_name, base=repository.default_branch, idempotency_key=f"pr:{job.id}")
        with self.database.session() as session:
            existing = session.scalar(select(PullRequest).where(PullRequest.job_id == job.id))
            if not existing:
                session.add(PullRequest(job_id=job.id, provider=repository.provider, provider_id=pr.provider_id, url=pr.url, is_draft=pr.is_draft))
            session.commit()
        await self.notifier.send(f"Push completado y draft PR creado: {pr.url}", chat_id=self._conversation_chat_id(), message_type="draft_pr_created", project_id=project.id, repository_id=repository.id, job_id=job.id)
        await self.notifier.send(f"Draft PR creado para {project.display_name}: {pr.url}", chat_id=self._status_chat_id(), message_type="draft_pr_created", project_id=project.id, repository_id=repository.id, job_id=job.id)
        self.jobs.transition(job.id, JobStatus.COMPLETED, {"pull_request_url": pr.url})

    async def _validate(self, job: Job, project: ProjectConfig, worktree_path: Path) -> list[Any]:
        self.jobs.transition(job.id, JobStatus.VALIDATING, {})
        cwd = self.config.resolve_project_path(project.id, worktree_path)
        results = [self.validator.run(command, cwd) for command in project.validation.commands]
        with self.database.session() as session:
            for result in results:
                session.add(ValidationRun(job_id=job.id, command=result.command, exit_code=result.exit_code, stdout_summary=result.stdout_summary, stderr_summary=result.stderr_summary, duration_seconds=result.duration_seconds, passed=result.passed, skipped=result.skipped, skip_reason=result.skip_reason))
            session.commit()
        return results

    async def _revalidate_stale_proposal(self, job: Job, project: ProjectConfig, repository: RepositoryConfig) -> bool:
        proposal_id = str(job.context.get("proposal_id", ""))
        if not proposal_id:
            return True
        with self.database.session() as session:
            proposal = session.get(Proposal, proposal_id)
        if not proposal or not proposal.base_sha:
            return True
        current_base = self.git.base_sha(repository)
        if current_base == proposal.base_sha:
            return True
        prompt = self._prompt(
            "proposal_revalidation",
            self._scope_context(project, repository, job, current_base)
            + f"\nProposal base SHA: {proposal.base_sha}\nCurrent base SHA: {current_base}\nProposal: {proposal.description}",
        )
        result = await self.codex.run(
            cwd=self.config.resolve_project_path(project.id),
            prompt=prompt,
            model=self.codex.choose_model(project, task="project_question"),
            mode="read_only",
            profile_path=project.codex.profile_path,
        )
        self._record_codex(job, result, "proposal_revalidation")
        valid = bool(result.structured and result.structured.model_extra and result.structured.model_extra.get("valid", False))
        if valid:
            return True
        self.jobs.transition(job.id, JobStatus.NEEDS_REVIEW, {"reason": "proposal_base_changed", "proposal_base_sha": proposal.base_sha, "current_base_sha": current_base})
        await self.notifier.send(
            f"La propuesta {proposal.id} quedó obsoleta porque cambió el base SHA. Se requiere revisión.",
            chat_id=self._conversation_chat_id(),
            message_type="proposal_stale",
            project_id=project.id,
            repository_id=repository.id,
            job_id=job.id,
        )
        return False

    async def _request_input_if_needed(self, job: Job, result: Any) -> bool:
        structured = result.structured
        if not structured or structured.result_type != "needs_input":
            return False
        questions = structured.questions or []
        if not questions:
            self.jobs.add_question(job.id, "free_text", "¿Qué información adicional debo usar?", "text")
            questions_text = "¿Qué información adicional debo usar?"
        else:
            for question in questions:
                self.jobs.add_question(job.id, question.id, question.question, question.type, question.options, question.required)
            questions_text = "\n".join(f"{question.id}: {question.question}" for question in questions)
        self.jobs.transition(job.id, JobStatus.AWAITING_INPUT, {"question_count": len(questions) or 1})
        await self.notifier.send(
            f"Necesito información adicional antes de continuar:\n{questions_text}",
            chat_id=self._conversation_chat_id(),
            message_type="input_request",
            job_id=job.id,
            execution_phase="awaiting_input",
            interaction_id=str(uuid.uuid4()),
        )
        return True

    def provider_for(self, repository: RepositoryConfig) -> SourceControlProvider | None:
        if repository.provider == "github" and repository.github:
            return GitHubProvider(repository.github.owner, repository.github.repository, repository.github.token_env)
        if repository.provider == "azure_devops" and repository.azure_devops:
            item = repository.azure_devops
            return AzureDevOpsProvider(item.organization, item.project, item.repository_id, item.pat_env)
        return None

    def _project_repository(self, job: Job) -> tuple[ProjectConfig, RepositoryConfig]:
        if not job.project_id:
            raise ValueError("Job has no project")
        project = self.config.project(job.project_id)
        return project, self.config.repository(project.repository)

    def _ensure_read_allowed(self, project: ProjectConfig) -> None:
        if not project.permissions.allow_read or not project.codex.enabled:
            raise PermissionError(f"Read/Codex execution disabled for project {project.id}")

    def _ensure_write_allowed(self, project: ProjectConfig) -> None:
        if not project.permissions.allow_changes or not project.codex.enabled:
            raise PermissionError(f"Changes/Codex execution disabled for project {project.id}")

    def _scope_context(self, project: ProjectConfig, repository: RepositoryConfig | None, job: Job, base_sha: str | None) -> str:
        return f"""Project: {project.id} ({project.display_name})
Repository: {repository.id if repository else project.repository}
Repository root: {repository.local_path if repository else 'not applicable'}
Working directory: {project.working_directory}
Allowed read paths: {project.scope.allowed_read_paths}
Allowed write paths: {project.scope.allowed_write_paths}
Forbidden paths: {project.scope.forbidden_paths}
Original request: {job.request_text}
Approved proposal: {job.context.get('proposal_id', '')}
User answers: {job.context.get('user_answers', [])}
Current phase: {job.phase or job.status}
Base SHA: {base_sha or 'unknown'}
Validation requirements: {project.validation.commands}
Return JSON with result_type in answer, analysis_result, proposals, needs_input, implementation_result, revision_result, diff_explanation, failure.
"""

    def _prompt(self, name: str, context: str) -> str:
        path = self.prompts_root / f"{name}.md"
        template = path.read_text(encoding="utf-8") if path.exists() else "{context}"
        return template.replace("{{context}}", context) + "\n\n" + context

    def _record_codex(self, job: Job, result: Any, phase: str) -> None:
        with self.database.session() as session:
            session.add(CodexExecution(id=result.execution_id, job_id=job.id, phase=phase, model=result.model.name, reasoning_effort=result.model.reasoning_effort, mode="read_only" if phase not in {"implementation", "revision"} else "workspace_write", session_id=result.session_id, command=result.command, exit_code=result.exit_code, stdout=result.stdout[-20000:], stderr=result.stderr[-10000:], result=result.structured.model_dump() if result.structured else None, finished_at=utcnow(), duration_seconds=result.duration_seconds, usage=result.structured.usage if result.structured else {}))
            session.commit()

    def _persist_proposals(self, job: Job, project: ProjectConfig, repository: RepositoryConfig, base_sha: str, values: list[dict[str, Any]]) -> list[Proposal]:
        created: list[Proposal] = []
        for index, value in enumerate(values, 1):
            proposal_id = str(value.get("id") or f"{project.id.upper()}-{value.get('category', 'FIX').upper()}-{utcnow().strftime('%Y%m%d')}-{index:03d}")
            from datetime import timedelta
            item = Proposal(id=proposal_id, job_id=job.id, project_id=project.id, repository_id=repository.id, review_id=job.id, category=str(value.get("category", "bug")), title=str(value.get("title", "Untitled proposal")), description=str(value.get("description", "")), evidence=list(value.get("evidence", [])), affected_files=list(value.get("affected_files", [])), scope_estimate=value.get("scope_estimate"), confidence=value.get("confidence"), risk=value.get("risk"), suggested_validation=list(value.get("suggested_validation", [])), likely_migration=bool(value.get("likely_migration", False)), shared_code_affected=bool(value.get("shared_code_affected", False)), base_sha=base_sha, expires_at=utcnow() + timedelta(days=7))
            with self.database.session() as session:
                existing = session.get(Proposal, proposal_id)
                if not existing:
                    session.add(item)
                    session.commit()
                    created.append(item)
        return created

    def _proposal_summary(self, project: ProjectConfig, proposals: list[Proposal]) -> str:
        return "\n\n".join(f"{item.id} [{item.category}] {item.title}\n{item.description}\nEvidence: {', '.join(item.evidence)}" for item in proposals)

    def _push_manifest(self, project: ProjectConfig, repository: RepositoryConfig, job: Job, worktree: Any, commits: list[tuple[str, str]], changes: list[Any], validations: list[Any]) -> str:
        validation = [f"{item.command}: {'passed' if item.passed else 'failed'}" for item in validations]
        branch_name = worktree.branch_name if hasattr(worktree, "branch_name") else worktree.branch
        return f"""Change ready for push review

Project: {project.id}
Job: {job.id}
Local branch: {branch_name}
Base branch: {repository.default_branch}
HEAD: {self.git.current_head(Path(worktree.path))}

Local commits: {len(commits)}
Files changed: {len(changes)}
Lines: +{sum(item.additions for item in changes)} / -{sum(item.deletions for item in changes)}

Validation:
{chr(10).join(f'- {item}' for item in validation) or '- no commands configured'}

Files:
{chr(10).join(f'- {item.path}' for item in changes) or '- no committed changes detected'}

The branch has not been pushed. Reply with an explicit approval such as “Push it” to authorize the exact HEAD.
"""

    def _worktree_for(self, job_id: str) -> Worktree | None:
        with self.database.session() as session:
            return session.scalar(select(Worktree).where(Worktree.job_id == job_id))

    def _approved_push(self, job_id: str) -> Any | None:
        from orchestrator.database.models import PushApproval
        with self.database.session() as session:
            return session.scalar(select(PushApproval).where(PushApproval.job_id == job_id, PushApproval.status == "approved").order_by(PushApproval.created_at.desc()))

    def _conversation_chat_id(self) -> str:
        return os.getenv(self.config.telegram.conversation_chat_id_env, "conversation")

    def _status_chat_id(self) -> str:
        return os.getenv(self.config.telegram.status_chat_id_env, "status")
