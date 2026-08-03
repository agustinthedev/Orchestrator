from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config.models import AppConfig, ProjectConfig, RepositoryConfig, ScopeConfig
from orchestrator.database.engine import Database
from orchestrator.database.models import Project, Repository
from orchestrator.jobs.service import JobService


@pytest.fixture
def database() -> Database:
    database = Database("sqlite:///:memory:")
    database.create_all()
    return database


@pytest.fixture
def jobs(database: Database) -> JobService:
    return JobService(database, approval_ttl_minutes=60)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    repository = RepositoryConfig(id="repo", display_name="Repo", local_path=tmp_path, default_branch="main")
    project = ProjectConfig(
        id="project",
        display_name="Project",
        repository="repo",
        scope=ScopeConfig(allowed_read_paths=["src"], allowed_write_paths=["src"], forbidden_paths=[".env"]),
    )
    return AppConfig(repositories=[repository], projects=[project])


@pytest.fixture
def seeded_config(database: Database, config: AppConfig) -> AppConfig:
    with database.session() as session:
        repository = config.repositories[0]
        project = config.projects[0]
        session.add(Repository(id=repository.id, display_name=repository.display_name, local_path=str(repository.local_path), default_branch=repository.default_branch))
        session.flush()
        session.add(Project(id=project.id, display_name=project.display_name, repository_id=repository.id, working_directory=project.working_directory, scope=project.scope.model_dump(), codex_config=project.codex.model_dump(), validation_config=project.validation.model_dump(), permissions=project.permissions.model_dump(), pipelines={}))
        session.commit()
    return config
