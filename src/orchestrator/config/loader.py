from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from orchestrator.config.models import (
    AppConfig,
)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def _read_collection(directory: Path, pattern: str, key: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    paths = sorted(directory.glob(pattern)) if directory.exists() else []
    concrete_paths = [path for path in paths if not path.name.endswith(".example.yaml")]
    for path in concrete_paths or paths:
        data = _read_yaml(path)
        value = data.get(key, data)
        if isinstance(value, list):
            values.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            values.append(value)
    return values


def _resolve_path(value: Path, base_dir: Path) -> Path:
    return value if value.is_absolute() else (base_dir / value).resolve()


def load_config(config_dir: Path | str = Path("config"), *, env: dict[str, str] | None = None) -> AppConfig:
    """Load typed non-secret YAML configuration; secrets are only read by name."""
    config_path = Path(config_dir).resolve()
    global_data = _read_yaml(config_path / "orchestrator.yaml")
    if not global_data:
        global_data = _read_yaml(config_path / "orchestrator.example.yaml")
    repositories_data = _read_collection(config_path, "repositories*.yaml", "repositories")
    project_data = _read_collection(config_path / "projects", "*.yaml", "projects")
    schedules_data = _read_collection(config_path, "schedules*.yaml", "schedules")

    values = dict(global_data)
    values["repositories"] = repositories_data or values.get("repositories", [])
    values["projects"] = project_data or values.get("projects", [])
    values["schedules"] = schedules_data or values.get("schedules", [])
    values["config_dir"] = config_path

    runtime = values.get("runtime", {})
    for key in ("worktrees_root", "logs_root", "temporary_root"):
        if key in runtime:
            runtime[key] = _resolve_path(Path(runtime[key]), config_path.parent)
    values["runtime"] = runtime

    config = AppConfig.model_validate(values)
    _ = env or os.environ
    return config


def configured_secret_names(config: AppConfig) -> set[str]:
    names = {
        config.telegram.bot_token_env,
        config.telegram.allowed_user_ids_env,
        config.telegram.allowed_chat_ids_env,
        config.telegram.conversation_chat_id_env,
        config.telegram.status_chat_id_env,
        config.telegram.transcription.api_key_env,
        config.telegram.intent_classification.api_key_env,
    }
    for repository in config.repositories:
        if repository.github:
            names.add(repository.github.token_env)
        if repository.azure_devops:
            names.add(repository.azure_devops.pat_env)
    return names
