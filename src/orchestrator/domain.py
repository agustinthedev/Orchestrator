from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePosixPath


class JobStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    INPUT_RECEIVED = "input_received"
    AWAITING_PROPOSAL_APPROVAL = "awaiting_proposal_approval"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    PREPARING_WORKTREE = "preparing_worktree"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    AWAITING_PUSH_APPROVAL = "awaiting_push_approval"
    REVISION_REQUESTED = "revision_requested"
    IMPLEMENTING_REVISION = "implementing_revision"
    PUSH_APPROVED = "push_approved"
    PUSHING = "pushing"
    CREATING_DRAFT_PR = "creating_draft_pr"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    NEEDS_REVIEW = "needs_review"


TERMINAL_STATES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.EXPIRED}

ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.CREATED: {JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.EXPIRED},
    JobStatus.RUNNING: {
        JobStatus.AWAITING_INPUT,
        JobStatus.AWAITING_PROPOSAL_APPROVAL,
        JobStatus.PREPARING_WORKTREE,
        JobStatus.VALIDATING,
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.NEEDS_REVIEW,
        JobStatus.PUSHING,
        JobStatus.IMPLEMENTING_REVISION,
    },
    JobStatus.AWAITING_INPUT: {JobStatus.INPUT_RECEIVED, JobStatus.EXPIRED, JobStatus.CANCELLED},
    JobStatus.INPUT_RECEIVED: {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.AWAITING_PROPOSAL_APPROVAL: {
        JobStatus.PROPOSAL_APPROVED,
        JobStatus.PROPOSAL_REJECTED,
        JobStatus.EXPIRED,
        JobStatus.CANCELLED,
    },
    JobStatus.PROPOSAL_APPROVED: {JobStatus.QUEUED, JobStatus.PREPARING_WORKTREE},
    JobStatus.PROPOSAL_REJECTED: {JobStatus.COMPLETED, JobStatus.CANCELLED},
    JobStatus.PREPARING_WORKTREE: {JobStatus.IMPLEMENTING, JobStatus.FAILED, JobStatus.NEEDS_REVIEW},
    JobStatus.IMPLEMENTING: {
        JobStatus.VALIDATING,
        JobStatus.AWAITING_INPUT,
        JobStatus.FAILED,
        JobStatus.NEEDS_REVIEW,
    },
    JobStatus.VALIDATING: {
        JobStatus.AWAITING_PUSH_APPROVAL,
        JobStatus.FAILED,
        JobStatus.NEEDS_REVIEW,
    },
    JobStatus.AWAITING_PUSH_APPROVAL: {
        JobStatus.PUSH_APPROVED,
        JobStatus.REVISION_REQUESTED,
        JobStatus.CANCELLED,
        JobStatus.EXPIRED,
    },
    JobStatus.REVISION_REQUESTED: {JobStatus.IMPLEMENTING_REVISION, JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.IMPLEMENTING_REVISION: {
        JobStatus.VALIDATING,
        JobStatus.AWAITING_INPUT,
        JobStatus.FAILED,
        JobStatus.NEEDS_REVIEW,
    },
    JobStatus.PUSH_APPROVED: {JobStatus.PUSHING, JobStatus.CANCELLED, JobStatus.NEEDS_REVIEW},
    JobStatus.PUSHING: {JobStatus.CREATING_DRAFT_PR, JobStatus.FAILED, JobStatus.NEEDS_REVIEW},
    JobStatus.CREATING_DRAFT_PR: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.NEEDS_REVIEW},
    JobStatus.NEEDS_REVIEW: {JobStatus.CANCELLED, JobStatus.QUEUED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: {JobStatus.QUEUED},
    JobStatus.CANCELLED: set(),
    JobStatus.EXPIRED: {JobStatus.QUEUED, JobStatus.CANCELLED},
}


class Intent(StrEnum):
    GLOBAL_QUESTION = "GLOBAL_QUESTION"
    PROJECT_QUESTION = "PROJECT_QUESTION"
    CODE_ANALYSIS = "CODE_ANALYSIS"
    PIPELINE_ANALYSIS = "PIPELINE_ANALYSIS"
    FEATURE_REQUEST = "FEATURE_REQUEST"
    FIX_REQUEST = "FIX_REQUEST"
    REFACTOR_REQUEST = "REFACTOR_REQUEST"
    TEST_REQUEST = "TEST_REQUEST"
    APPROVE_PROPOSAL = "APPROVE_PROPOSAL"
    REJECT_PROPOSAL = "REJECT_PROPOSAL"
    ASK_ABOUT_CHANGE = "ASK_ABOUT_CHANGE"
    REQUEST_DIFF_DETAIL = "REQUEST_DIFF_DETAIL"
    REQUEST_REVISION = "REQUEST_REVISION"
    APPROVE_PUSH = "APPROVE_PUSH"
    REJECT_PUSH = "REJECT_PUSH"
    CANCEL_JOB = "CANCEL_JOB"
    SHOW_STATUS = "SHOW_STATUS"
    UNKNOWN = "UNKNOWN"


def classify_intent(text: str, *, state: str | None = None, has_reply_context: bool = False) -> Intent:
    value = text.casefold().strip()
    if not value:
        return Intent.UNKNOWN
    if re.search(r"\b(push|go ahead and push|create (the )?pr|approved for push)\b", value) and not re.search(
        r"\b(why|what|which|explain)\b", value
    ):
        return Intent.APPROVE_PUSH
    if re.search(r"\b(reject|decline|do not push|don't push|cancel push)\b", value):
        return Intent.REJECT_PUSH
    if re.search(r"\b(cancel|stop|abort)\b", value):
        return Intent.CANCEL_JOB
    if re.search(r"\b(approve|approved|yes|implement it|do it)\b", value) and state in {
        "awaiting_proposal_approval",
        "proposal_approved",
    }:
        return Intent.APPROVE_PROPOSAL
    if re.search(r"\b(reject|not this|no thanks)\b", value) and state == "awaiting_proposal_approval":
        return Intent.REJECT_PROPOSAL
    if re.search(r"\b(remove|drop|revert|change|revise|keep only|instead)\b", value) and has_reply_context:
        return Intent.REQUEST_REVISION
    if re.search(r"\b(why|explain|what changed|which files|diff)\b", value) and has_reply_context:
        return Intent.ASK_ABOUT_CHANGE
    if re.search(r"\b(pipeline|build|workflow|ci|nightly|run failed|logs?)\b", value):
        return Intent.PIPELINE_ANALYSIS
    if re.search(r"\b(schedule|scheduler|worktree|orchestrator|jobs? waiting|health|credential)\b", value):
        return Intent.GLOBAL_QUESTION
    if re.search(r"\b(review|analy[sz]e|find (the )?function|where is|how does|repository|repo)\b", value):
        return Intent.PROJECT_QUESTION
    if re.search(r"\b(refactor|restructure|clean up)\b", value):
        return Intent.REFACTOR_REQUEST
    if re.search(r"\b(test|coverage|spec)\b", value):
        return Intent.TEST_REQUEST
    if re.search(r"\b(add|implement|introduce|support|feature)\b", value):
        return Intent.FEATURE_REQUEST
    if re.search(r"\b(fix|bug|broken|failure|error|incorrect)\b", value):
        return Intent.FIX_REQUEST
    return Intent.UNKNOWN


def sanitize_branch_name(value: str, *, prefix: str = "change") -> str:
    normalized = value.strip().replace("_", "-")
    normalized = re.sub(r"[^A-Za-z0-9./-]+", "-", normalized)
    normalized = re.sub(r"/{2,}", "/", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip(".-/")
    if not normalized:
        normalized = "job"
    if not normalized.startswith(f"{prefix}/"):
        normalized = f"{prefix}/{normalized}"
    if normalized in {"main", "master", "develop", "trunk"} or normalized.endswith("/"):
        normalized = f"{prefix}/job"
    return normalized[:240].rstrip(".-/")


def normalize_relative_path(path: str) -> str:
    candidate = path.replace("\\", "/").strip()
    candidate = str(PurePosixPath(candidate))
    while candidate.startswith("./"):
        candidate = candidate[2:]
    return "." if candidate in {"", "."} else candidate


def path_matches(path: str, configured: str) -> bool:
    normalized = normalize_relative_path(path)
    scope = normalize_relative_path(configured)
    return scope == "." or normalized == scope or normalized.startswith(f"{scope}/")


def paths_are_allowed(
    changed_paths: Iterable[str],
    *,
    allowed_write_paths: Iterable[str],
    forbidden_paths: Iterable[str],
) -> tuple[bool, list[str]]:
    allowed = list(allowed_write_paths)
    forbidden = list(forbidden_paths)
    violations: list[str] = []
    for path in changed_paths:
        if any(path_matches(path, item) for item in forbidden) or not any(
            path_matches(path, item) for item in allowed
        ):
            violations.append(normalize_relative_path(path))
    return not violations, violations


def redact_secrets(text: str, secret_values: Iterable[str] = ()) -> str:
    redacted = text
    for secret in secret_values:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"(?i)(token|password|pat|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", redacted)
    return redacted
