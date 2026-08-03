from orchestrator.providers.azure_devops import AzureDevOpsProvider
from orchestrator.providers.github import GitHubProvider


def test_github_request_is_draft(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    calls = []

    class Response:
        status_code = 201
        text = ""

        def json(self):
            return {"number": 3, "html_url": "https://github/pr/3", "draft": True}

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()

    monkeypatch.setattr("orchestrator.providers.github.httpx.request", request)
    provider = GitHubProvider("owner", "repo")
    result = provider.create_draft_pull_request(title="t", body="b", head="fix/a", base="main", idempotency_key="x")
    assert result.is_draft
    payload = calls[-1][2]["json"]
    assert payload["draft"] is True


def test_azure_request_is_draft(monkeypatch) -> None:
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "secret-pat")
    calls = []

    class Response:
        status_code = 201
        text = ""

        def json(self):
            return {"pullRequestId": 4, "url": "https://azure/pr/4", "isDraft": True}

    monkeypatch.setattr("orchestrator.providers.azure_devops.httpx.request", lambda method, url, **kwargs: (calls.append((method, url, kwargs)) or Response()))
    provider = AzureDevOpsProvider("org", "project", "repo")
    result = provider.create_draft_pull_request(title="t", body="b", head="fix/a", base="main", idempotency_key="x")
    assert result.is_draft
    assert calls[-1][2]["json"]["isDraft"] is True

