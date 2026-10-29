"""One edited comment per kind on a pull request (JAIF-416)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from aiflow.v4.control.github import GitHubApp
from aiflow.v4.core import secretbox


def github(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFLOW4_SECRET_KEY", secretbox.generate_key())
    app_dir = tmp_path / "github-app"
    app_dir.mkdir()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    (app_dir / "app.json").write_text(json.dumps({"id": "901", "account_login": "acme", "installation_id": 77}))
    (app_dir / "app.pem").write_text(pem)
    comments: list[dict] = []
    calls: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/access_tokens"):
            expires = (datetime.now(timezone.utc) + timedelta(minutes=55)).isoformat()
            return httpx.Response(201, json={"token": "ghs_test", "expires_at": expires})
        if request.method == "GET" and request.url.path == "/repos/acme/repo/issues/7/comments":
            return httpx.Response(200, json=comments)
        if request.method == "POST" and request.url.path == "/repos/acme/repo/issues/7/comments":
            comments.append({"id": len(comments) + 1, "body": json.loads(request.content)["body"]})
            return httpx.Response(201, json=comments[-1])
        if request.method == "PATCH" and request.url.path.startswith("/repos/acme/repo/issues/comments/"):
            comment = next(c for c in comments if str(c["id"]) == request.url.path.rsplit("/", 1)[1])
            comment["body"] = json.loads(request.content)["body"]
            return httpx.Response(200, json=comment)
        return httpx.Response(404, json={"message": "not found"})

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handle))
    return GitHubApp(str(app_dir), "acme", client), comments, calls


def test_the_first_result_is_created_and_later_ones_edit_the_same_comment(tmp_path, monkeypatch):
    app, comments, calls = github(tmp_path, monkeypatch)
    assert app.upsert_comment("repo", 7, "verify", "AI Flow verify: FAIL (round 1)") == "created"
    assert app.upsert_comment("repo", 7, "verify", "AI Flow verify: FAIL (round 2)") == "updated"
    assert app.upsert_comment("repo", 7, "verify", "AI Flow verify: PASS") == "updated"
    assert len(comments) == 1 and "PASS" in comments[0]["body"] and "round" not in comments[0]["body"]
    assert comments[0]["body"].startswith("<!-- aiflow:verify -->")


def test_an_identical_result_makes_no_write(tmp_path, monkeypatch):
    app, comments, calls = github(tmp_path, monkeypatch)
    app.upsert_comment("repo", 7, "verify", "same")
    before = len(calls)
    assert app.upsert_comment("repo", 7, "verify", "same") == "updated"
    assert [c for c in calls[before:] if c[0] in ("POST", "PATCH") and "access_tokens" not in c[1]] == []


def test_each_kind_has_its_own_comment_and_other_comments_are_left_alone(tmp_path, monkeypatch):
    app, comments, calls = github(tmp_path, monkeypatch)
    comments.append({"id": 100, "body": "a human wrote this"})
    app.upsert_comment("repo", 7, "verify", "verify one")
    app.upsert_comment("repo", 7, "triage", "triage one")
    app.upsert_comment("repo", 7, "verify", "verify two")
    bodies = [c["body"] for c in comments]
    assert len(comments) == 3 and bodies[0] == "a human wrote this"
    assert any("verify two" in b for b in bodies) and any("triage one" in b for b in bodies)


def test_the_repository_ships_a_quiet_coderabbit_configuration():
    import yaml
    from pathlib import Path
    config = yaml.safe_load((Path(__file__).resolve().parents[2] / ".coderabbit.yaml").read_text())
    reviews = config["reviews"]
    assert reviews["profile"] == "quiet" and reviews["high_level_summary"] is False
    assert reviews["review_status"] is False and reviews["changed_files_summary"] is False
