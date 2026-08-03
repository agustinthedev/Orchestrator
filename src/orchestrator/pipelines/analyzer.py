from __future__ import annotations

from enum import StrEnum
from typing import Any

from orchestrator.providers.base import PipelineRunInfo, SourceControlProvider


class FailureClass(StrEnum):
    APPLICATION_CODE = "application_code_failure"
    TEST = "test_failure"
    FLAKY_TEST = "flaky_test"
    INFRASTRUCTURE = "infrastructure_failure"
    DISCONNECTED_DEVICE = "disconnected_device"
    TIMEOUT = "timeout"
    CREDENTIALS = "credentials_issue"
    DEPENDENCY = "unavailable_dependency"
    CONFIGURATION = "configuration_issue"
    UNKNOWN = "unknown"


PipelineDiagnostic = dict[str, Any]


class PipelineAnalyzer:
    def __init__(self, provider: SourceControlProvider, *, max_log_chars: int = 20000) -> None:
        self.provider = provider
        self.max_log_chars = max_log_chars

    def latest_diagnostic(self, pipeline_id: str) -> PipelineDiagnostic | None:
        run = self.provider.latest_pipeline_run(pipeline_id)
        if not run:
            return None
        if run.conclusion in {None, "success", "succeeded"} and run.status not in {"failed", "completed"}:
            return {"run": run, "successful": True, "failure_class": None, "logs": ""}
        logs = self.provider.pipeline_logs(run.external_id, max_chars=self.max_log_chars)
        failure = classify_failure(logs, run)
        return {
            "run": run,
            "successful": run.conclusion in {"success", "succeeded"},
            "failure_class": failure.value,
            "logs": logs,
            "evidence": evidence_for_failure(failure, logs),
            "code_fix_supported": failure in {FailureClass.APPLICATION_CODE, FailureClass.TEST, FailureClass.CONFIGURATION},
        }


def classify_failure(logs: str, run: PipelineRunInfo | None = None) -> FailureClass:
    value = logs.casefold()
    if any(token in value for token in ("credential", "unauthorized", "401", "forbidden", "secret")):
        return FailureClass.CREDENTIALS
    if any(token in value for token in ("timed out", "timeout", "deadline exceeded")):
        return FailureClass.TIMEOUT
    if any(token in value for token in ("device offline", "device not found", "adb", "simulator unavailable")):
        return FailureClass.DISCONNECTED_DEVICE
    if any(token in value for token in ("could not resolve", "package not found", "module not found", "dependency")):
        return FailureClass.DEPENDENCY
    if any(token in value for token in ("flaky", "intermittent", "passes on retry")):
        return FailureClass.FLAKY_TEST
    if any(token in value for token in ("pytest", "test failed", "assertionerror", "test failure")):
        return FailureClass.TEST
    if any(token in value for token in ("yaml", "configuration", "invalid config", "missing setting")):
        return FailureClass.CONFIGURATION
    if any(token in value for token in ("runner", "agent", "disk full", "service unavailable", "infrastructure")):
        return FailureClass.INFRASTRUCTURE
    if run and run.conclusion in {"failure", "failed"}:
        return FailureClass.APPLICATION_CODE
    return FailureClass.UNKNOWN


def evidence_for_failure(failure: FailureClass, logs: str) -> list[str]:
    keywords = {
        FailureClass.CREDENTIALS: ("credential", "unauthorized", "401", "forbidden"),
        FailureClass.TIMEOUT: ("timeout", "timed out"),
        FailureClass.TEST: ("pytest", "assertionerror", "test failed"),
        FailureClass.CONFIGURATION: ("configuration", "invalid config"),
    }.get(failure, ())
    return [line[-500:] for line in logs.splitlines() if any(keyword in line.casefold() for keyword in keywords)][-10:]
