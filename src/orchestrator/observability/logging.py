from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from orchestrator.domain import redact_secrets


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
        }
        for key in ("correlation_id", "job_id", "project_id", "repository_id", "execution_id", "event_type"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(logs_root: Path, *, level: int = logging.INFO) -> None:
    logs_root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs_root / "orchestrator.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    formatter = JsonFormatter()
    handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)
    root.addHandler(console)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

