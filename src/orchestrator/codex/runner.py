from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orchestrator.config.models import CodexSettings, ModelSpec, ProjectConfig
from orchestrator.domain import redact_secrets


class CodexQuestion(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    question: str
    type: str = "text"
    options: list[dict[str, Any]] = Field(default_factory=list)
    required: bool = True


class StructuredCodexResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    result_type: str
    answer: str | None = None
    summary: str | None = None
    questions: list[CodexQuestion] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    commits: list[dict[str, Any]] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    session_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class CodexResult:
    execution_id: str
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    structured: StructuredCodexResult | None
    duration_seconds: float
    session_id: str | None
    model: ModelSpec


class CodexRunner:
    """Disposable Codex process runner. State is supplied by the caller on each run."""

    def __init__(self, settings: CodexSettings) -> None:
        self.settings = settings

    def choose_model(self, project: ProjectConfig | None = None, *, task: str = "default") -> ModelSpec:
        name = project.codex.model_override if project and project.codex.model_override else None
        effort = project.codex.reasoning_effort_override if project and project.codex.reasoning_effort_override else self.settings.task_reasoning_effort.get(task)
        spec = ModelSpec(
            name=name or self.settings.default_model.name,
            reasoning_effort=effort or self.settings.default_model.reasoning_effort,
        )
        if spec.name not in self.settings.allowed_models:
            raise ValueError(f"Model is not allowed by configuration: {spec.name}")
        if spec.reasoning_effort not in self.settings.allowed_reasoning_efforts:
            raise ValueError(f"Reasoning effort is not allowed: {spec.reasoning_effort}")
        return spec

    def build_command(self, model: ModelSpec, *, mode: str, prompt: str, resume_session: str | None = None) -> list[str]:
        command = [self.settings.executable, "exec", "--json", "--model", model.name]
        if mode == "read_only":
            command.extend(["--sandbox", "read-only"])
        elif mode == "workspace_write":
            command.extend(["--sandbox", "workspace-write"])
        else:
            raise ValueError(f"Unsupported Codex mode: {mode}")
        command.extend(self.settings.extra_args)
        if resume_session:
            command.extend(["--resume", resume_session])
        command.append(prompt)
        return command

    async def run(
        self,
        *,
        cwd: Path,
        prompt: str,
        model: ModelSpec,
        mode: str,
        profile_path: Path | None = None,
        resume_session: str | None = None,
        timeout_seconds: int = 3600,
        env: dict[str, str] | None = None,
    ) -> CodexResult:
        command = self.build_command(model, mode=mode, prompt=prompt, resume_session=resume_session)
        execution_id = str(uuid.uuid4())
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        if profile_path:
            process_env["CODEX_HOME"] = str(profile_path.resolve())
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=process_env,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError(f"Codex execution timed out after {timeout_seconds}s") from None
        stdout = redact_secrets(stdout_bytes.decode(errors="replace"))
        stderr = redact_secrets(stderr_bytes.decode(errors="replace"))
        structured = self.parse_structured_output(stdout)
        session_id = structured.session_id if structured else None
        return CodexResult(
            execution_id=execution_id,
            command=command,
            exit_code=process.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            structured=structured,
            duration_seconds=time.monotonic() - started,
            session_id=session_id,
            model=model,
        )

    @staticmethod
    def parse_structured_output(stdout: str) -> StructuredCodexResult | None:
        candidates = [stdout.strip()]
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            candidates.append(stripped)
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = item.get("text")
                if isinstance(message, str) and message.strip():
                    candidates.append(message.strip())
        for candidate in reversed(candidates):
            try:
                value = json.loads(candidate)
                if isinstance(value, dict) and "result_type" in value:
                    return StructuredCodexResult.model_validate(value)
            except (json.JSONDecodeError, ValidationError):
                continue
        return None
