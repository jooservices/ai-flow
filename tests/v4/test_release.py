import hashlib
from pathlib import Path

from aiflow.v4.control import api as control_api
from aiflow.v4.core.release_notes import git_commits, render_changelog


def _join(client, name):
    response = client.post("/v1/nodes/join", json={"name": name, "join_token": "join-secret"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def test_tag_release_workflow_builds_signs_and_publishes_assets():
    workflow = Path(".github/workflows/release-v4.yml").read_text()
    for required in (
        'tags: ["v*"]',
        "needs: v4-tests",
        "uv run --frozen pytest tests/v4 -q",
        "git merge-base --is-ancestor",
        "git archive --format=tar.gz",
        "SHA256SUMS",
        "cosign sign-blob",
        "cosign sign --yes",
        "docker push",
        "gh release create",
        "aiflow4-$VERSION.tar.gz.sigstore.json",
    ):
        assert required in workflow


def test_conventional_release_notes_group_commits_and_mark_breaking_changes():
    notes = render_changelog([
        ("abcdef012345", "feat(api): serve signed versions", ""),
        ("123456789abc", "fix(node): reject tampered archive", ""),
        ("987654321abc", "feat!: require signed release", "BREAKING CHANGE: old commit ids are no longer installed"),
        ("222222222222", "update release workflow", ""),
    ])
    assert notes.index("## Breaking Changes") < notes.index("## Features") < notes.index("## Fixes")
    assert "**api:** serve signed versions (abcdef0)" in notes
    assert "old commit ids are no longer installed" in notes
    assert "update release workflow (2222222)" in notes


def test_git_changelog_reads_commits_between_release_tags(tmp_path, monkeypatch):
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "release-test@example.com")
    git("config", "user.name", "Release Test")
    (tmp_path / "file.txt").write_text("first\n")
    git("add", "file.txt")
    git("commit", "-q", "-m", "chore: start")
    git("tag", "v1.0.0")
    (tmp_path / "file.txt").write_text("second\n")
    git("commit", "-qam", "feat(api): add release endpoint")
    git("tag", "v1.1.0")
    monkeypatch.chdir(tmp_path)

    commits = git_commits("v1.0.0", "v1.1.0")

    assert len(commits) == 1
    assert len(commits[0][0]) == 40
    assert commits[0][1:] == ("feat(api): add release endpoint", "")

    first_release_commits = git_commits(None, "v1.1.0")
    assert len(first_release_commits) == 2


def test_node_release_api_serves_version_tarball_checksum_and_signature(client, tmp_path, monkeypatch):
    release_dir = tmp_path / "v4.2.1"
    release_dir.mkdir()
    tarball = b"versioned release archive"
    digest = hashlib.sha256(tarball).hexdigest()
    filename = "aiflow4-v4.2.1.tar.gz"
    (release_dir / filename).write_bytes(tarball)
    (release_dir / f"{filename}.sigstore.json").write_text('{"verificationMaterial":{}}')
    (release_dir / "SHA256SUMS").write_text(f"{digest}  {filename}\n")
    (tmp_path / "CURRENT").write_text("v4.2.1\n")
    monkeypatch.setattr(control_api, "RELEASES", tmp_path)
    headers = _join(client, "node-release")

    heartbeat = client.post("/v1/nodes/node-release/heartbeat", json={}, headers=headers)
    assert heartbeat.json()["desired"]["release"] == "v4.2.1"
    assert client.get("/v1/releases/v4.2.1/manifest").status_code == 401

    manifest_response = client.get("/v1/releases/v4.2.1/manifest", headers=headers)
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest == {
        "version": "v4.2.1",
        "sha256": digest,
        "tarball": "/v1/releases/v4.2.1.tar.gz",
        "signature": "/v1/releases/v4.2.1.tar.gz.sigstore.json",
        "checksums": "/v1/releases/v4.2.1/SHA256SUMS",
        "filename": filename,
    }
    assert client.get(manifest["tarball"], headers=headers).content == tarball
    assert client.get(manifest["signature"], headers=headers).json() == {"verificationMaterial": {}}
    assert client.get(manifest["checksums"], headers=headers).text == f"{digest}  {filename}\n"


def test_node_release_failure_opens_incident(client, ctx):
    headers = _join(client, "node-release")
    payload = {"release_version": "v4.2.1", "detail": "cosign rejected the signature"}
    response = client.post("/v1/nodes/node-release/release-failures", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"recorded": True, "opened": True}
    incident = ctx.db.one("SELECT severity, kind, subject, title, detail FROM incidents WHERE closed_at IS NULL")
    assert incident["severity"] == "critical"
    assert incident["kind"] == "release_verification"
    assert incident["subject"] == "node-release:v4.2.1"
    assert "rejected release v4.2.1" in incident["title"]
    assert incident["detail"] == payload["detail"]


def test_release_installer_verifies_before_unpacking_or_switching_current():
    installer = Path("deploy/v4/bin/aiflow4-node-install").read_text()
    assert installer.index("cosign verify-blob") < installer.index('tar -xzf "$TAR"')
    assert installer.index("cosign verify-blob") < installer.index('ln -sfn "$REL"')

