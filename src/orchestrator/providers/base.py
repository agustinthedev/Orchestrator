from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class PullRequestResult:
    provider_id: str
    url: str
    is_draft: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class PipelineRunInfo:
    external_id: str
    status: str
    conclusion: str | None
    url: str | None
    metadata: dict[str, Any]


class SourceControlProvider(Protocol):
    def repository_metadata(self) -> dict[str, Any]: ...

    def create_draft_pull_request(
        self, *, title: str, body: str, head: str, base: str, idempotency_key: str
    ) -> PullRequestResult: ...

    def get_pull_request(self, provider_id: str) -> dict[str, Any]: ...

    def add_pull_request_comment(self, provider_id: str, body: str) -> dict[str, Any]: ...

    def latest_pipeline_run(self, pipeline_id: str) -> PipelineRunInfo | None: ...

    def pipeline_logs(self, run_id: str, *, max_chars: int = 20000) -> str: ...


def draft_pr_description(
    *,
    summary: str,
    problem: str,
    changes: list[str],
    validation: list[str],
    risk: str,
    limitations: list[str],
    files: list[str],
    proposal_id: str | None,
    commits: list[str],
    base_sha: str,
    head_sha: str,
) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) if items else "- None reported"

    return f"""## Summary

{summary}

## Problem

{problem}

## Changes

{bullets(changes)}

## Validation

{bullets(validation)}

## Risk

{risk}

## Known limitations

{bullets(limitations)}

## Files of interest

{bullets(files)}

## Source

- Proposal: `{proposal_id or 'manual request'}`
- Base commit: `{base_sha}`
- Approved local HEAD: `{head_sha}`

## Commits

{bullets(commits)}

## Reviewer notes

Please review the scope and validation evidence before publishing this draft pull request.
"""
