from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config.models import GitSettings, RepositoryConfig, ScopeConfig, is_within
from orchestrator.domain import paths_are_allowed, sanitize_branch_name


class GitError(RuntimeError):
    pass


class PushRefused(GitError):
    pass


@dataclass(frozen=True)
class GitCommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class WorktreeInfo:
    id: str
    path: Path
    branch: str
    base_sha: str


@dataclass(frozen=True)
class FileChangeInfo:
    path: str
    change_type: str
    additions: int
    deletions: int
    summary: str = ""


class GitManager:
    def __init__(self, settings: GitSettings, worktrees_root: Path) -> None:
        self.settings = settings
        self.worktrees_root = worktrees_root

    def run(self, repository_path: Path, *args: str, check: bool = True) -> GitCommandResult:
        command = ["git", *args]
        completed = subprocess.run(
            command,
            cwd=repository_path,
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
        result = GitCommandResult(command, completed.returncode, completed.stdout, completed.stderr)
        if check and completed.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({completed.returncode}): {completed.stderr[-2000:]}")
        return result

    def ensure_clean(self, repository: RepositoryConfig) -> None:
        result = self.run(repository.local_path, "status", "--porcelain")
        if result.stdout.strip():
            raise GitError(f"Repository checkout is not clean: {repository.local_path}")

    def fetch(self, repository: RepositoryConfig) -> None:
        self.run(repository.local_path, "fetch", "--prune", repository.remote.name)

    def base_sha(self, repository: RepositoryConfig, *, fetch: bool = True) -> str:
        if fetch:
            self.fetch(repository)
        result = self.run(repository.local_path, "rev-parse", f"{repository.remote.name}/{repository.default_branch}")
        return result.stdout.strip()

    def create_worktree(
        self,
        repository: RepositoryConfig,
        *,
        job_id: str,
        proposal_id: str,
        prefix: str = "fix",
    ) -> WorktreeInfo:
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        base_sha = self.base_sha(repository)
        branch = sanitize_branch_name(proposal_id or job_id, prefix=prefix)
        branch = f"{branch}-{job_id[:8]}"
        path = (self.worktrees_root / repository.id / job_id).resolve()
        if not is_within(path, self.worktrees_root.resolve()):
            raise GitError("Worktree path escaped configured worktree root")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run(repository.local_path, "branch", branch, base_sha)
        self.run(repository.local_path, "worktree", "add", str(path), branch)
        return WorktreeInfo(str(uuid.uuid4()), path, branch, base_sha)

    def current_head(self, worktree_path: Path) -> str:
        return self.run(worktree_path, "rev-parse", "HEAD").stdout.strip()

    def changed_files(self, worktree_path: Path, base_sha: str) -> list[FileChangeInfo]:
        result = self.run(worktree_path, "diff", "--numstat", "--find-renames", f"{base_sha}..HEAD")
        changes: list[FileChangeInfo] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            changes.append(
                FileChangeInfo(
                    path=path,
                    change_type="modified",
                    additions=int(added) if added.isdigit() else 0,
                    deletions=int(deleted) if deleted.isdigit() else 0,
                )
            )
        return changes

    def status_changed_files(self, worktree_path: Path) -> list[str]:
        result = self.run(worktree_path, "status", "--porcelain")
        return [line[3:].strip() for line in result.stdout.splitlines() if len(line) > 3]

    def commits_since(self, worktree_path: Path, base_sha: str) -> list[tuple[str, str]]:
        result = self.run(worktree_path, "log", "--format=%H%x09%s", f"{base_sha}..HEAD")
        return [tuple(line.split("\t", 1)) for line in result.stdout.splitlines() if "\t" in line]

    def enforce_scope(self, worktree_path: Path, base_sha: str, scope: ScopeConfig) -> tuple[bool, list[str]]:
        changes = self.changed_files(worktree_path, base_sha)
        paths = [change.path for change in changes]
        return paths_are_allowed(
            paths,
            allowed_write_paths=scope.allowed_write_paths,
            forbidden_paths=scope.forbidden_paths,
        )

    def push_after_approval(
        self,
        repository: RepositoryConfig,
        worktree_path: Path,
        *,
        branch: str,
        default_branch: str,
        approved_head_sha: str,
    ) -> str:
        if not self.settings.require_push_approval:
            raise PushRefused("Push approval cannot be disabled")
        if branch == default_branch or branch == repository.default_branch:
            raise PushRefused("Direct push to the default branch is forbidden")
        if branch.startswith("-"):
            raise PushRefused("Invalid branch name")
        if not self.settings.allow_force_push:
            pass
        actual = self.current_head(worktree_path)
        if self.settings.verify_approved_head_sha and actual != approved_head_sha:
            raise PushRefused(f"HEAD changed after approval: expected {approved_head_sha}, got {actual}")
        self.run(worktree_path, "push", "--no-force", repository.remote.name, f"HEAD:refs/heads/{branch}")
        return actual

    def remove_worktree(self, repository: RepositoryConfig, worktree_path: Path, *, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        self.run(repository.local_path, *args, str(worktree_path))

