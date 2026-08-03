from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidationResult:
    command: str
    exit_code: int | None
    stdout_summary: str
    stderr_summary: str
    duration_seconds: float
    passed: bool
    skipped: bool = False
    skip_reason: str | None = None


class ValidationRunner:
    def __init__(self, *, timeout_seconds: int = 1800, output_limit: int = 4000) -> None:
        self.timeout_seconds = timeout_seconds
        self.output_limit = output_limit

    def run(self, command: str, cwd: Path) -> ValidationResult:
        start = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                shell=True,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            return ValidationResult(
                command=command,
                exit_code=result.returncode,
                stdout_summary=result.stdout[-self.output_limit :],
                stderr_summary=result.stderr[-self.output_limit :],
                duration_seconds=time.monotonic() - start,
                passed=result.returncode == 0,
            )
        except subprocess.TimeoutExpired as exc:
            return ValidationResult(
                command=command,
                exit_code=None,
                stdout_summary=str(exc.stdout or "")[-self.output_limit :],
                stderr_summary="Validation timed out",
                duration_seconds=time.monotonic() - start,
                passed=False,
            )

