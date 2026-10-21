import hashlib
import shutil
import subprocess
from pathlib import Path

from aiflow.v4.control import api as control_api
from aiflow.v4.core.release_notes import git_commits, render_changelog
from conftest import join_node


def _join(client, name):
    response = join_node(client, name)
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
    # the image reference is passed to the signing step through the environment under the name that step reads
    assert 'echo "IMAGE=$IMAGE" >> "$GITHUB_ENV"' in workflow and 'cosign sign --yes "$IMAGE"' in workflow


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

    heartbeat = client.post("/v1/nodes/node-release/heartbeat", json={"release_protocol": 1}, headers=headers)
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
    assert installer.index("cosign verify-blob") < installer.index('install -m 0755 "$REL/deploy/v4/bin/aiflow4-node-install')
    assert installer.index("cosign verify-blob") < installer.index('ln -sfn "$REL"')


# --- the installer and auto-deployer, run against stub commands ----------------------------------------------

INSTALLER = Path("deploy/v4/bin/aiflow4-node-install").resolve()
AUTODEPLOY = Path("deploy/v4/bin/aiflow4-autodeploy").resolve()
_SYSTEM_TOOLS = ("bash", "cat", "awk", "sha256sum", "dirname", "grep", "uname", "rm", "mv", "ln", "mkdir", "head",
                 "printf", "touch")


def _bin(tmp_path, stubs: dict[str, str]) -> str:
    """A PATH with only a few system tools and the stubs, so a cosign on the test machine is never used."""
    d = tmp_path / "bin"
    d.mkdir()
    for tool in _SYSTEM_TOOLS:
        if found := shutil.which(tool):
            (d / tool).symlink_to(found)
    for name, body in {"sleep": "exit 0\n", **stubs}.items():
        (d / name).write_text("#!/bin/bash\n" + body)
        (d / name).chmod(0o755)
    return str(d)


def _legacy_node(tmp_path, version="v4.2.1"):
    """What a node agent from before signed releases leaves: only the downloaded tarball."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    tar = incoming / f"aiflow4-{version}.tar.gz"
    tar.write_bytes(b"signed release")
    (tmp_path / "node.token").write_text("secret-node-token\n")
    (tmp_path / "node.env").write_text("AIFLOW4_CONTROL_URL=http://control.test\nAIFLOW4_NODE_NAME=node-old\n"
                                       f"AIFLOW4_NODE_TOKEN_FILE={tmp_path / 'node.token'}\n")
    return tar


# curl stub: logs arguments and the config read on stdin for installer incident reporting
_CURL = """
LOG={log}
echo "ARGS $*" >> "$LOG"; echo "STDIN $(cat)" >> "$LOG"
url="${{@: -1}}"
case "$url" in */release-failures) ;; *) exit 22 ;; esac
"""


def _run_installer(tmp_path, tar, version, stubs, bundle=None, checksum=None):
    env = {"PATH": _bin(tmp_path, stubs), "AIFLOW4_NODE_ENV": str(tmp_path / "node.env")}
    args = ["bash", str(INSTALLER), str(tar), version]
    if bundle is not None:
        args.extend([str(bundle), checksum])
    return subprocess.run(args, env=env, capture_output=True, text=True,
                          timeout=60)


def test_installer_legacy_two_argument_call_requires_manual_bootstrap_and_reports_incident(tmp_path):
    tar = _legacy_node(tmp_path)
    log = tmp_path / "curl.log"
    stubs = {"curl": _CURL.format(log=log, digest="unused", name=tar.name),
             "cosign": f'touch {tmp_path / "cosign.ran"}\n'}

    result = _run_installer(tmp_path, tar, "v4.2.1", stubs)

    assert result.returncode == 4, result.stderr
    assert "legacy automatic upgrade is disabled" in result.stderr
    calls = log.read_text()
    assert "/v1/releases/" not in calls
    assert "-X POST" in calls and "http://control.test/v1/nodes/node-old/release-failures" in calls
    assert '"release_version": "v4.2.1"' in calls
    # the node token reaches curl on stdin, never on its command line
    assert all("secret-node-token" not in line for line in calls.splitlines() if line.startswith("ARGS"))
    assert 'header = "Authorization: Bearer secret-node-token"' in calls
    assert not (tmp_path / "cosign.ran").exists()


def test_installer_four_argument_call_verifies_signature_before_unpacking(tmp_path):
    tar = _legacy_node(tmp_path)
    bundle = Path(f"{tar}.sigstore.json")
    bundle.write_text("{}")
    log = tmp_path / "curl.log"
    digest = hashlib.sha256(tar.read_bytes()).hexdigest()
    stubs = {"curl": _CURL.format(log=log, digest="unused", name=tar.name),
             "cosign": f'echo "$*" > {tmp_path / "cosign.args"}; echo "invalid signature" >&2; exit 1\n'}

    result = _run_installer(tmp_path, tar, "v4.2.1", stubs, bundle, digest)

    assert result.returncode == 4
    assert "release signature verification failed for v4.2.1" in result.stderr
    cosign_args = (tmp_path / "cosign.args").read_text()
    assert f"verify-blob {tar} --bundle {bundle}" in cosign_args
    assert "release-v4.yml@refs/tags/v4.2.1" in cosign_args
    assert "release-failures" in log.read_text()


def test_installer_four_argument_call_without_cosign_is_rejected_loudly_with_an_incident(tmp_path):
    tar = _legacy_node(tmp_path)
    bundle = Path(f"{tar}.sigstore.json")
    bundle.write_text("{}")
    log = tmp_path / "curl.log"

    result = _run_installer(tmp_path, tar, "v4.2.1", {"curl": _CURL.format(log=log, digest="unused", name=tar.name)},
                            bundle, hashlib.sha256(tar.read_bytes()).hexdigest())

    assert result.returncode == 4
    assert "cosign is required" in result.stderr
    assert "aiflow4-node-install <release.tar.gz> v4.2.1 <release.sigstore.json> <sha256>" in result.stderr
    assert "/v1/nodes/node-old/release-failures" in log.read_text()


def test_installer_four_argument_call_rejects_checksum_mismatch_before_signature_check(tmp_path):
    tar = _legacy_node(tmp_path)
    bundle = Path(f"{tar}.sigstore.json")
    bundle.write_text("{}")
    log = tmp_path / "curl.log"
    stubs = {"curl": _CURL.format(log=log, digest="unused", name=tar.name),
             "cosign": f"touch {tmp_path / 'cosign.ran'}\n"}

    result = _run_installer(tmp_path, tar, "v4.2.1", stubs, bundle, "0" * 64)

    assert result.returncode == 4
    assert "release checksum verification failed for v4.2.1" in result.stderr
    assert "release-failures" in log.read_text()
    assert not (tmp_path / "cosign.ran").exists()


def test_installer_rejects_unversioned_or_partial_arguments(tmp_path):
    env = {"PATH": _bin(tmp_path, {})}
    for args in (["x.tar.gz", "abc1234def"], ["x.tar.gz", "v4.2.1", "bundle.json"]):
        result = subprocess.run(["bash", str(INSTALLER), *args], env=env, capture_output=True, text=True)
        assert result.returncode == 2


def _autodeploy(tmp_path, current, image_present=True, build_present=True):
    root = tmp_path / "srv"
    releases = root / "control" / "releases"
    releases.mkdir(parents=True)
    (releases / "CURRENT").write_text(current + "\n")
    if build_present:
        compose = root / "build" / current / "deploy" / "v4" / "control" / "compose.yaml"
        compose.parent.mkdir(parents=True)
        compose.write_text("services: {}\n")
    log = tmp_path / "calls.log"
    log.write_text("")
    stubs = {
        "git": 'case "$*" in *"tag --merged"*) echo v4.1.0 ;; esac\n',
        "gh": "printf 'aiflow4-v4.1.0.tar.gz\\naiflow4-v4.1.0.tar.gz.sigstore.json\\nSHA256SUMS\\n'\n",
        # the new release deploys and writes CURRENT, then fails its health checks; a rollback deploy succeeds
        "aiflow4-deploy": f'echo "deploy $*" >> {log}\n'
                          f'[ "$1" = v4.1.0 ] && echo v4.1.0 > {releases}/CURRENT\nexit 0\n',
        "curl": "exit 7\n",
        "docker": f'echo "docker $* tag=${{AIFLOW4_IMAGE_TAG:-}} repo=${{AIFLOW4_IMAGE_REPOSITORY:-}}" >> {log}\n'
                  f'[ "$1" = image ] && exit {0 if image_present else 1}\nexit 0\n',
        "systemctl": f'echo "systemctl $*" >> {log}\n',
        "python3": f'[ "$1" = - ] && {{ cat >/dev/null; echo "alert $2" >> {log}; }}\nexit 0\n',
    }
    env = {"PATH": _bin(tmp_path, stubs), "AIFLOW4_ROOT": str(root)}
    result = subprocess.run(["bash", str(AUTODEPLOY)], env=env, capture_output=True, text=True, timeout=60)
    return result, log.read_text(), (releases / "CURRENT").read_text().strip()


def test_autodeploy_rolls_back_a_commit_id_release_from_its_kept_image(tmp_path):
    result, calls, current = _autodeploy(tmp_path, "abc1234def")

    assert result.returncode == 1, result.stdout + result.stderr
    for svc in ("control-api-a", "control-api-b", "control-loops"):
        assert f"up -d --no-deps {svc} tag=abc1234def repo=aiflow4/control" in calls
    assert current == "abc1234def"
    assert "alert [CRITICAL] AI Flow: auto-deploy of v4.1.0 rolled back" in calls


def test_autodeploy_alert_says_no_rollback_when_the_commit_id_release_is_gone(tmp_path):
    result, calls, _ = _autodeploy(tmp_path, "abc1234def", image_present=False)

    assert result.returncode == 1
    assert "up -d" not in calls
    assert "alert [CRITICAL] AI Flow: auto-deploy of v4.1.0 failed, NOT rolled back" in calls
    assert "no rollback happened" in result.stdout


def test_autodeploy_rolls_back_a_versioned_release_with_aiflow4_deploy(tmp_path):
    result, calls, _ = _autodeploy(tmp_path, "v4.0.0", build_present=False)

    assert result.returncode == 1
    assert calls.index("deploy v4.1.0") < calls.index("deploy v4.0.0")
    assert "alert [CRITICAL] AI Flow: auto-deploy of v4.1.0 rolled back" in calls



def test_develop_channel_scripts_refuse_hosts_that_did_not_opt_in(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "AIFLOW4_NODE_ENV": str(tmp_path / "missing.env")}
    for script, args in (("aiflow4-develop-install", ["x.tar.gz", "0123456789"]), ("aiflow4-deploy-develop", [])):
        done = subprocess.run(["bash", f"deploy/v4/bin/{script}", *args], capture_output=True, text=True, env=env)
        assert done.returncode == 3 and "not on the develop channel" in done.stderr
    autodeploy = Path("deploy/v4/bin/aiflow4-autodeploy").read_text()
    assert 'AIFLOW4_CHANNEL:-release}" = develop ] && exec /usr/local/sbin/aiflow4-autodeploy-develop' in autodeploy
