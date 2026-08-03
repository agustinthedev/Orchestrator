from __future__ import annotations

import shutil
from collections.abc import Callable
from typing import Any

from orchestrator.database.engine import Database


def health_snapshot(
    database: Database,
    *,
    telegram_connected: bool,
    scheduler_running: bool,
    worker_count: int,
    queued_jobs: Callable[[], int] | None = None,
    active_jobs: Callable[[], int] | None = None,
    codex_executable: str = "codex",
) -> dict[str, Any]:
    try:
        database_ok = database.healthcheck()
    except Exception:
        database_ok = False
    return {
        "application_running": True,
        "telegram_connected": telegram_connected,
        "scheduler_running": scheduler_running,
        "worker_count": worker_count,
        "queued_jobs": queued_jobs() if queued_jobs else None,
        "active_jobs": active_jobs() if active_jobs else None,
        "database_accessible": database_ok,
        "codex_executable_available": shutil.which(codex_executable) is not None,
        "git_executable_available": shutil.which("git") is not None,
    }


def format_health(snapshot: dict[str, Any]) -> str:
    lines = ["Orchestrator health"]
    for key, value in snapshot.items():
        lines.append(f"- {key.replace('_', ' ')}: {value}")
    return "\n".join(lines)

