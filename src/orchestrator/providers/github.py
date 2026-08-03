from __future__ import annotations

import os
from typing import Any

import httpx

from orchestrator.providers.base import PipelineRunInfo, PullRequestResult


class GitHubProvider:
    def __init__(self, owner: str, repository: str, token_env: str = "GITHUB_TOKEN", *, timeout: float = 20) -> None:
        self.owner = owner
        self.repository = repository
        self.token_env = token_env
        self.timeout = timeout
        self.base_url = f"https://api.github.com/repos/{owner}/{repository}"

    def _headers(self) -> dict[str, str]:
        token = os.getenv(self.token_env)
        if not token:
            raise RuntimeError(f"GitHub credential is not configured: {self.token_env}")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = httpx.request(method, url, headers=self._headers(), timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API request failed ({response.status_code}): {response.text[:1000]}")
        return response

    def create_draft_pull_request(
        self, *, title: str, body: str, head: str, base: str, idempotency_key: str
    ) -> PullRequestResult:
        existing = self._find_by_head(head)
        if existing:
            return PullRequestResult(str(existing["number"]), existing["html_url"], bool(existing.get("draft", True)), existing)
        response = self._request(
            "POST",
            f"{self.base_url}/pulls",
            json={"title": title, "body": body, "head": head, "base": base, "draft": True},
        )
        data = response.json()
        if not data.get("draft", False):
            raise RuntimeError("GitHub did not create the pull request as a draft")
        return PullRequestResult(str(data["number"]), data["html_url"], True, data)

    def _find_by_head(self, head: str) -> dict[str, Any] | None:
        response = self._request("GET", f"{self.base_url}/pulls", params={"state": "open", "head": f"{self.owner}:{head}"})
        items = response.json()
        if not isinstance(items, list):
            return None
        return items[0] if items else None

    def get_pull_request(self, provider_id: str) -> dict[str, Any]:
        return self._request("GET", f"{self.base_url}/pulls/{provider_id}").json()

    def add_pull_request_comment(self, provider_id: str, body: str) -> dict[str, Any]:
        return self._request("POST", f"{self.base_url}/issues/{provider_id}/comments", json={"body": body}).json()

    def latest_pipeline_run(self, pipeline_id: str) -> PipelineRunInfo | None:
        response = self._request("GET", f"{self.base_url}/actions/workflows/{pipeline_id}/runs", params={"per_page": 1})
        runs = response.json().get("workflow_runs", [])
        if not runs:
            return None
        run = runs[0]
        return PipelineRunInfo(str(run["id"]), run.get("status", "unknown"), run.get("conclusion"), run.get("html_url"), run)

    def pipeline_logs(self, run_id: str, *, max_chars: int = 20000) -> str:
        response = self._request("GET", f"{self.base_url}/actions/runs/{run_id}/logs")
        return response.text[:max_chars]
