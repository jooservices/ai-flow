from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from aiflow.v4.control.github import GitHubApp
from aiflow.v4.core import secretbox


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("AIFLOW4_SECRET_KEY", secretbox.generate_key())


def github_with_open_pr(tmp_path, title: str):
    app_dir = tmp_path / "github-app"
    app_dir.mkdir()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    (app_dir / "app.json").write_text(json.dumps({"id": "901", "account_login": "acme", "installation_id": 77}))
    (app_dir / "app.pem").write_text(pem)

    current_pr = {"number": 42, "html_url": "https://github.com/acme/repo/pull/42", "title": title}
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path.endswith("/access_tokens"):
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=55)).isoformat()
            return httpx.Response(201, json={"token": "ghs_test", "expires_at": expires_at})
        if request.method == "GET" and request.url.path == "/repos/acme/repo/pulls":
            return httpx.Response(200, json=[dict(current_pr)])
        if request.method == "PATCH" and request.url.path == "/repos/acme/repo/pulls/42":
            current_pr["title"] = json.loads(request.content)["title"]
            return httpx.Response(200, json=dict(current_pr))
        return httpx.Response(404, json={"message": "not found"})

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handle))
    return GitHubApp(str(app_dir), "acme", client), current_pr, requests


def test_create_pr_updates_existing_open_pr_when_title_changes(tmp_path):
    github, current_pr, requests = github_with_open_pr(tmp_path, "feat: old Jira summary (TESTFLOW-94)")

    pr = github.create_pr("repo", "TESTFLOW-94-update-summary", "develop",
                          "feat: updated Jira summary (TESTFLOW-94)", "body")

    patches = [request for request in requests if request.method == "PATCH"]
    assert len(patches) == 1
    assert patches[0].url.path == "/repos/acme/repo/pulls/42"
    assert json.loads(patches[0].content) == {"title": "feat: updated Jira summary (TESTFLOW-94)"}
    assert current_pr["title"] == "feat: updated Jira summary (TESTFLOW-94)"
    assert pr["title"] == current_pr["title"]


def test_create_pr_does_not_patch_existing_open_pr_when_title_matches(tmp_path):
    title = "feat: current Jira summary (TESTFLOW-94)"
    github, current_pr, requests = github_with_open_pr(tmp_path, title)

    pr = github.create_pr("repo", "TESTFLOW-94-update-summary", "develop", title, "body")

    assert not [request for request in requests if request.method == "PATCH"]
    assert current_pr["title"] == title
    assert pr["title"] == title
