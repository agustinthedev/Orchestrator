from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.codex.runner import CodexResult, StructuredCodexResult
from orchestrator.config.models import (
    AppConfig,
    ProjectConfig,
    RepositoryConfig,
    ScopeConfig,
    ValidationConfig,
)
from orchestrator.database.models import Project, Repository
from orchestrator.domain import JobStatus
from orchestrator.git.manager import GitManager
from orchestrator.providers.base import PullRequestResult
from orchestrator.validation import ValidationRunner
from orchestrator.workflows.engine import OrchestratorEngine


class FakeCodex:
    def __init__(self, *, forbidden: bool = False, commit: bool = True) -> None:
        self.forbidden = forbidden
        self.commit = commit

    def choose_model(self, project=None, *, task="default"):
        from orchestrator.config.models import ModelSpec

        return ModelSpec(name="fake", reasoning_effort="high")

    async def run(self, *, cwd, prompt, model, mode, profile_path=None, **kwargs):
        target = Path(cwd)
        path = target / (".env" if self.forbidden else "src/change.py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n", encoding="utf-8")
        if self.commit:
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=target, check=True)
            subprocess.run(["git", "add", "."], cwd=target, check=True)
            subprocess.run(["git", "commit", "-m", "fix: add scoped change"], cwd=target, check=True, capture_output=True)
        return CodexResult(
            execution_id="fake-execution",
            command=["fake-codex"],
            exit_code=0,
            stdout='{"result_type":"implementation_result"}',
            stderr="",
            structured=StructuredCodexResult(result_type="implementation_result"),
            duration_seconds=0.01,
            session_id=None,
            model=model,
        )


class CaptureNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str, *, chat_id: str, message_type: str, **kwargs) -> str:
        self.messages.append(text)
        return "telegram-message"


class FakeProvider:
    def create_draft_pull_request(self, *, title, body, head, base, idempotency_key):
        return PullRequestResult("42", "https://provider.example/pull/42", True, {"draft": True})


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def create_clone(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    (seed / "src").mkdir()
    (seed / "src" / "existing.py").write_text("value = 0\n", encoding="utf-8")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "config", "user.name", "Test")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "chore: initial")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "origin", "main")
    subprocess.run(["git", "clone", "--branch", "main", str(bare), str(clone)], check=True, capture_output=True)
    return clone


def setup_config(tmp_path: Path, repo_path: Path, *, forbidden: bool = False) -> AppConfig:
    repository = RepositoryConfig(id="repo", display_name="Repo", local_path=repo_path, default_branch="main")
    project = ProjectConfig(
        id="project",
        display_name="Project",
        repository="repo",
        scope=ScopeConfig(allowed_read_paths=["src"], allowed_write_paths=["src"], forbidden_paths=[".env"]),
        validation=ValidationConfig(commands=["python -c \"print('validation ok')\""] if not forbidden else []),
    )
    return AppConfig(repositories=[repository], projects=[project], runtime={"worktrees_root": tmp_path / "worktrees"})


@pytest.mark.asyncio
async def test_write_job_uses_isolated_worktree_and_stops_before_push(tmp_path, database, jobs) -> None:
    repo_path = create_clone(tmp_path)
    config = setup_config(tmp_path, repo_path)
    with database.session() as session:
        session.add(Repository(id="repo", display_name="Repo", local_path=str(repo_path), default_branch="main"))
        session.flush()
        session.add(Project(id="project", display_name="Project", repository_id="repo", scope=config.projects[0].scope.model_dump(), codex_config=config.projects[0].codex.model_dump(), validation_config=config.projects[0].validation.model_dump(), permissions=config.projects[0].permissions.model_dump(), pipelines={}))
        session.commit()
    job = jobs.create_job(kind="implementation", idempotency_key="write-flow", project_id="project", repository_id="repo", request_text="Add a scoped change", context={})
    claimed = jobs.claim_next()
    assert claimed is not None
    notifier = CaptureNotifier()
    engine = OrchestratorEngine(config, database, jobs, FakeCodex(), GitManager(config.git, config.runtime.worktrees_root), ValidationRunner(), notifier=notifier)
    await engine.handle_job(claimed)
    stored = jobs.get(job.id)
    assert stored is not None
    assert stored.status == "awaiting_push_approval"
    assert stored.worktree_path
    assert Path(stored.worktree_path).resolve() != repo_path.resolve()
    assert stored.branch_name == "fix/add-feature"
    assert jobs.pending_push_approval(job.id) is not None
    assert any("Push it" in message for message in notifier.messages)
    assert git(repo_path, "rev-parse", "HEAD") == git(repo_path, "rev-parse", "origin/main")

    jobs.approve_push(job.id, stored.head_sha, "42", "approval-message")
    jobs.update_context(job.id, {"push_requested": True})
    jobs.transition(job.id, JobStatus.PUSH_APPROVED)
    engine.provider_for = lambda repository: FakeProvider()
    await engine.push(jobs.get(job.id))
    assert jobs.get(job.id).status == "completed"
    assert git(repo_path, "ls-remote", "origin", stored.branch_name).strip()
    with database.session() as session:
        from orchestrator.database.models import PullRequest

        pull_request = session.query(PullRequest).filter(PullRequest.job_id == job.id).one()
        assert pull_request.is_draft


@pytest.mark.asyncio
async def test_write_job_finalizes_uncommitted_codex_changes(tmp_path, database, jobs) -> None:
    repo_path = create_clone(tmp_path)
    config = setup_config(tmp_path, repo_path)
    with database.session() as session:
        session.add(Repository(id="repo", display_name="Repo", local_path=str(repo_path), default_branch="main"))
        session.flush()
        session.add(Project(id="project", display_name="Project", repository_id="repo", scope=config.projects[0].scope.model_dump(), codex_config=config.projects[0].codex.model_dump(), validation_config=config.projects[0].validation.model_dump(), permissions=config.projects[0].permissions.model_dump(), pipelines={}))
        session.commit()
    job = jobs.create_job(kind="implementation", idempotency_key="write-flow-uncommitted", project_id="project", repository_id="repo", request_text="Add a scoped change", context={})
    claimed = jobs.claim_next()
    assert claimed is not None
    engine = OrchestratorEngine(config, database, jobs, FakeCodex(commit=False), GitManager(config.git, config.runtime.worktrees_root), ValidationRunner(), notifier=CaptureNotifier())

    await engine.handle_job(claimed)

    stored = jobs.get(job.id)
    assert stored is not None
    assert stored.status == "awaiting_push_approval"
    assert git(Path(stored.worktree_path), "log", "-1", "--format=%s") == "orchestrator: apply approved change"
    assert git(Path(stored.worktree_path), "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_forbidden_path_moves_job_to_needs_review(tmp_path, database, jobs) -> None:
    repo_path = create_clone(tmp_path)
    config = setup_config(tmp_path, repo_path, forbidden=True)
    with database.session() as session:
        session.add(Repository(id="repo", display_name="Repo", local_path=str(repo_path), default_branch="main"))
        session.flush()
        session.add(Project(id="project", display_name="Project", repository_id="repo", scope=config.projects[0].scope.model_dump(), codex_config=config.projects[0].codex.model_dump(), validation_config=config.projects[0].validation.model_dump(), permissions=config.projects[0].permissions.model_dump(), pipelines={}))
        session.commit()
    job = jobs.create_job(kind="implementation", idempotency_key="forbidden-flow", project_id="project", repository_id="repo", request_text="Do change", context={})
    claimed = jobs.claim_next()
    assert claimed is not None
    engine = OrchestratorEngine(config, database, jobs, FakeCodex(forbidden=True), GitManager(config.git, config.runtime.worktrees_root), ValidationRunner(), notifier=CaptureNotifier())
    await engine.handle_job(claimed)
    assert jobs.get(job.id).status == "needs_review"
    assert jobs.pending_push_approval(job.id) is None
