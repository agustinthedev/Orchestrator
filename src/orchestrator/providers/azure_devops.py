from __future__ import annotations

import base64
import os
from typing import Any, cast

import httpx

from orchestrator.providers.base import PipelineRunInfo, PullRequestResult


class AzureDevOpsProvider:
    def __init__(
        self,
        organization: str,
        project: str,
        repository_id: str,
        pat_env: str = "AZURE_DEVOPS_PAT",
        *,
        timeout: float = 20,
    ) -> None:
        self.organization = organization
        self.project = project
        self.repository_id = repository_id
        self.pat_env = pat_env
        self.timeout = timeout
        self.base_url = f"https://dev.azure.com/{organization}"

    def _headers(self) -> dict[str, str]:
        pat = os.getenv(self.pat_env)
        if not pat:
            raise RuntimeError(f"Azure DevOps credential is not configured: {self.pat_env}")
        encoded = base64.b64encode(f":{pat}".encode()).decode()
        return {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        params = kwargs.pop("params", {})
        params.setdefault("api-version", "7.1")
        response = httpx.request(method, url, headers=self._headers(), params=params, timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"Azure DevOps API request failed ({response.status_code}): {response.text[:1000]}")
        return response

    def create_draft_pull_request(
        self, *, title: str, body: str, head: str, base: str, idempotency_key: str
    ) -> PullRequestResult:
        existing = self._find_by_head(head)
        if existing:
            return PullRequestResult(str(existing["pullRequestId"]), existing["url"], bool(existing.get("isDraft", True)), existing)
        url = f"{self.base_url}/{self.project}/_apis/git/repositories/{self.repository_id}/pullrequests"
        response = self._request(
            "POST",
            url,
            json={
                "sourceRefName": f"refs/heads/{head}",
                "targetRefName": f"refs/heads/{base}",
                "title": title,
                "description": body,
                "isDraft": True,
            },
        )
        data = response.json()
        if not data.get("isDraft", False):
            raise RuntimeError("Azure DevOps did not create the pull request as a draft")
        return PullRequestResult(str(data["pullRequestId"]), data["url"], True, data)

    def repository_metadata(self) -> dict[str, Any]:
        url = f"{self.base_url}/{self.project}/_apis/git/repositories/{self.repository_id}"
        return cast(dict[str, Any], self._request("GET", url).json())

    def _find_by_head(self, head: str) -> dict[str, Any] | None:
        url = f"{self.base_url}/{self.project}/_apis/git/repositories/{self.repository_id}/pullrequests"
        response = self._request("GET", url, params={"searchCriteria.sourceRefName": f"refs/heads/{head}", "searchCriteria.status": "active"})
        items = response.json().get("value", [])
        return items[0] if items else None

    def get_pull_request(self, provider_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/{self.project}/_apis/git/repositories/{self.repository_id}/pullrequests/{provider_id}"
        return cast(dict[str, Any], self._request("GET", url).json())

    def add_pull_request_comment(self, provider_id: str, body: str) -> dict[str, Any]:
        url = f"{self.base_url}/{self.project}/_apis/git/repositories/{self.repository_id}/pullrequests/{provider_id}/threads"
        return cast(dict[str, Any], self._request("POST", url, json={"comments": [{"content": body, "commentType": 1}]}).json())

    def latest_pipeline_run(self, pipeline_id: str) -> PipelineRunInfo | None:
        url = f"{self.base_url}/{self.project}/_apis/build/builds"
        response = self._request("GET", url, params={"definitions": pipeline_id, "$top": 1, "queryOrder": "finishTimeDescending"})
        items = response.json().get("value", [])
        if not items:
            return None
        run = items[0]
        return PipelineRunInfo(str(run["id"]), run.get("status", "unknown"), run.get("result"), run.get("_links", {}).get("web", {}).get("href"), run)

    def pipeline_logs(self, run_id: str, *, max_chars: int = 20000) -> str:
        url = f"{self.base_url}/{self.project}/_apis/build/builds/{run_id}/logs"
        response = self._request("GET", url)
        return response.text[:max_chars]
