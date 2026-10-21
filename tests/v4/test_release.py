import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiflow.v4.control import api as control_api
from aiflow.v4.control.canary import (Thresholds, decide as decide_canary, is_infrastructure_failure,
                                      measure_registry, record_promotion, record_rollback, summarize_runs)
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


def test_canary_heartbeat_targets_only_one_node_while_current_stays_previous(client, tmp_path, monkeypatch):
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "CURRENT").write_text("v4.2.0\n")
    (releases / "CANARY.json").write_text(json.dumps({"node": "canary", "target": "v4.3.0"}))
    monkeypatch.setattr(control_api, "RELEASES", releases)
    canary_auth = _join(client, "canary")
    fleet_auth = _join(client, "fleet")

    canary = client.post("/v1/nodes/canary/heartbeat", json={"release_protocol": 1}, headers=canary_auth)
    fleet = client.post("/v1/nodes/fleet/heartbeat", json={"release_protocol": 1}, headers=fleet_auth)

    assert canary.json()["desired"]["release"] == "v4.3.0"
    assert fleet.json()["desired"]["release"] == "v4.2.0"


class _FakeCanaryRegistry:
    """Small release registry double with old-fleet and candidate-node run history."""

    def __init__(self, nodes, runs):
        self.nodes = nodes
        self.runs = runs

    def one(self, sql, params):
        if "JOIN nodes n" in sql:
            previous, node, started_at, _ = params
            rows = [r for r in self.runs if (r["node"] == node or self.nodes.get(r["node"]) == previous)
                    and started_at - timedelta(hours=24) <= r["started_at"] < started_at
                    and r["status"] != "running"]
        else:
            node, started_at = params
            rows = [r for r in self.runs if r["node"] == node and r["started_at"] >= started_at
                    and r["status"] != "running"]
        return {"runs": len(rows), "infra_failures": sum(is_infrastructure_failure(r) for r in rows)}


def test_canary_compares_two_releases_in_fake_registry_and_rolls_back_at_threshold():
    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=21)
    runs = []
    for i in range(10):
        runs.append({"node": "stable", "started_at": started - timedelta(hours=1) + timedelta(minutes=i),
                     "status": "finished", "outcome": "failed" if i < 3 else "done",
                     "error_kind": "provider" if i < 3 else ("quota" if i == 3 else "")})
    for i, (outcome, error_kind) in enumerate((
        ("failed", "worker"), ("blocked", "infra"), ("lost", ""), ("failed", "quota"), ("done", ""),
    )):
        runs.append({"node": "canary", "started_at": started + timedelta(minutes=i), "status": "finished",
                     "outcome": outcome, "error_kind": error_kind})
    registry = _FakeCanaryRegistry({"stable": "v4.2.0", "canary": "v4.3.0"}, runs)

    measurements = measure_registry(registry, "canary", started, "v4.2.0")
    decision = decide_canary(measurements, 21 * 60, Thresholds(), installed=True)

    assert measurements["baseline"] == {"runs": 10, "infra_failures": 3, "failure_rate": 0.3}
    assert measurements["canary"] == {"runs": 5, "infra_failures": 3, "failure_rate": 0.6}
    assert decision["action"] == "rollback"
    two_failures = {"canary": {"runs": 5, "infra_failures": 2, "failure_rate": 0.4},
                    "baseline": {"runs": 10, "infra_failures": 0, "failure_rate": 0.0}}
    assert decide_canary(two_failures, 21 * 60, Thresholds(), installed=True)["action"] == "promote"
    assert is_infrastructure_failure({"outcome": "blocked", "error_kind": "infra"})
    assert not is_infrastructure_failure({"outcome": "failed", "error_kind": "quota"})


def test_canary_waits_for_both_minimums_and_promotes_at_minimum_or_maximum_age():
    policy = Thresholds.from_values(20 * 60, 5, 60 * 60, "2", 3)
    four_runs = {"canary": summarize_runs([{"outcome": "done"}] * 4),
                 "baseline": summarize_runs([{"outcome": "done"}] * 10)}
    five_runs = {"canary": summarize_runs([{"outcome": "done"}] * 5),
                 "baseline": summarize_runs([{"outcome": "done"}] * 10)}

    assert decide_canary(four_runs, 20 * 60, policy)["action"] == "wait"
    assert decide_canary(five_runs, 20 * 60 - 1, policy)["action"] == "wait"
    assert decide_canary(five_runs, 20 * 60, policy)["action"] == "promote"
    late = decide_canary(four_runs, 60 * 60, policy)
    assert late["action"] == "promote"
    assert "too few" in late["reason"]


def test_canary_rollback_opens_incident_and_flow_bug_with_suspected_commit():
    class DB:
        def __init__(self):
            self.calls = []

        def one(self, sql, params=None):
            self.calls.append((sql, params))
            return {"id": 9} if "INSERT INTO incidents" in sql else None

        def run(self, sql, params=None):
            self.calls.append((sql, params))
            return 1

    class Jira:
        def __init__(self):
            self.created = None

        def create_issue(self, project, summary, description, issuetype, labels):
            self.created = (project, summary, description, issuetype, labels)
            return "TESTFLOW-999"

        def status(self, key):
            return "Backlog"

    class Events:
        def emit(self, *args, **kwargs):
            return None

    db, jira = DB(), Jira()
    ctx = SimpleNamespace(db=db, jira=jira, events=Events(),
                          settings=SimpleNamespace(canary_bug_project="TESTFLOW", public_url="http://test"))
    result = record_rollback(ctx, node="canary", target="v4.3.0", previous="v4.2.0", commit="abcdef0123456789",
                             measurements={"canary": {"runs": 5, "infra_failures": 3, "failure_rate": 0.6},
                                           "baseline": {"runs": 10, "infra_failures": 1, "failure_rate": 0.1}})

    assert result == {"incident_opened": True, "ticket": "TESTFLOW-999"}
    assert "abcdef0123456789" in jira.created[2]
    assert jira.created[3:] == ("Bug", ["flow-bug"])
    assert any("INSERT INTO incidents" in call[0] and "critical" in call[1] for call in db.calls)


def test_under_observed_promotion_records_the_run_count_and_configured_minimum():
    class DB:
        def __init__(self):
            self.calls = []

        def one(self, sql, params=None):
            self.calls.append((sql, params))
            return {"id": 10} if "INSERT INTO incidents" in sql else None

        def run(self, sql, params=None):
            self.calls.append((sql, params))
            return 1

    class Events:
        def emit(self, *args, **kwargs):
            return None

    db = DB()
    ctx = SimpleNamespace(db=db, events=Events(), settings=SimpleNamespace(public_url="http://test"))
    opened = record_promotion(ctx, target="v4.3.0", node="canary",
                              measurements={"canary": {"runs": 2, "infra_failures": 0, "failure_rate": 0.0}},
                              under_observed=True, required_runs=5)

    incident = next(params for sql, params in db.calls if "INSERT INTO incidents" in sql)
    assert opened is True
    assert incident[2] == "info"
    assert "Completed runs: 2" in incident[4]
    assert "Required completed runs: 5" in incident[4]


def test_release_installer_verifies_before_unpacking_or_switching_current():
    installer = Path("deploy/v4/bin/aiflow4-node-install").read_text()
    assert installer.index("cosign verify-blob") < installer.index('tar -xzf "$TAR"')
    assert installer.index("cosign verify-blob") < installer.index('install -m 0755 "$REL/deploy/v4/bin/aiflow4-node-install')
    assert installer.index("cosign verify-blob") < installer.index('ln -sfn "$REL"')
    assert installer.index('mv -T /srv/aiflow/current.new') < installer.index('systemctl restart "${WORKERS[@]}"')
    assert "systemctl list-units --state=active" in installer


def test_release_deployer_stage_only_exits_before_control_and_current_promotion():
    deployer = Path("deploy/v4/bin/aiflow4-deploy").read_text()
    stage_only = deployer.index('if [ "$STAGE_ONLY" = yes ]; then')

    assert stage_only > deployer.index('mv "$STAGED" "$RELEASES/$VERSION"')
    assert stage_only < deployer.index('CF="$B/deploy/v4/control/compose.yaml"')
    assert stage_only < deployer.index('printf \'%s\\n\' "$VERSION" > "$RELEASES/CURRENT.tmp"')


# --- the installer and auto-deployer, run against stub commands ----------------------------------------------

INSTALLER = Path("deploy/v4/bin/aiflow4-node-install").resolve()
AUTODEPLOY = Path("deploy/v4/bin/aiflow4-autodeploy").resolve()
_SYSTEM_TOOLS = ("bash", "cat", "awk", "sha256sum", "dirname", "grep", "uname", "rm", "mv", "ln", "mkdir", "head",
                 "printf", "touch", "flock")


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


def _canary_autodeploy(tmp_path, *, active=False, decision=None, now=None):
    root = tmp_path / "srv"
    releases = root / "control" / "releases"
    releases.mkdir(parents=True)
    (releases / "CURRENT").write_text("v4.2.0\n")
    log = tmp_path / "calls.log"
    log.write_text("")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"nodes": [{"name": "canary", "status": "online",
                                            "release": "v4.3.0" if active else "v4.2.0",
                                            "accounts_assigned": 1},
                                           {"name": "worker-2", "status": "online", "release": "v4.2.0",
                                            "accounts_assigned": 1}]}))
    if active:
        started = now or datetime.now(timezone.utc) - timedelta(minutes=21)
        (releases / "CANARY.json").write_text(json.dumps({"target": "v4.3.0", "node": "canary",
                                                           "previous": "v4.2.0", "started_at": started.isoformat()}))
    if decision is None:
        decision = {"action": "promote", "reason": "minimum canary time and completed run count reached",
                    "canary": {"runs": 5, "infra_failures": 0, "failure_rate": 0.0},
                    "baseline": {"runs": 10, "infra_failures": 0, "failure_rate": 0.0}}
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(decision))
    py = (f'if [ "$1" = - ] && [ "$#" = 3 ]; then echo "alert $2" >> {log}; cat >/dev/null; exit 0; fi\n'
          f'exec {sys.executable} "$@"\n')
    stubs = {
        "git": 'case "$*" in *"tag --merged"*) echo v4.3.0 ;; *"rev-list"*) echo abcdef0123456789 ;; esac\n',
        "gh": "printf 'aiflow4-v4.3.0.tar.gz\\naiflow4-v4.3.0.tar.gz.sigstore.json\\nSHA256SUMS\\n'\n",
        "aiflow4-deploy": f'echo "deploy $*" >> {log}\n'
                          f'if [ "$1" = --stage-only ]; then exit 0; fi\n'
                          f'echo "$1" > {releases}/CURRENT\nexit 0\n',
        "docker": f'echo "docker $*" >> {log}\n'
                  f'case "$*" in *"canary evaluate"*) cat {decision_file} ;; esac\nexit 0\n',
        "curl": f'case "$*" in *"/v1/state"*) cat {state} ;; */healthz*|*/cc*) exit 0 ;; *) exit 7 ;; esac\n',
        "python3": py,
        "sleep": "exit 0\n",
    }
    env = {"PATH": _bin(tmp_path, stubs), "AIFLOW4_ROOT": str(root)}
    result = subprocess.run(["bash", str(AUTODEPLOY)], env=env, capture_output=True, text=True, timeout=60)
    canary_file = releases / "CANARY.json"
    current = (releases / "CURRENT").read_text().strip()
    return result, log.read_text(), current, json.loads(canary_file.read_text()) if canary_file.exists() else None, releases


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


def test_autodeploy_stages_candidate_for_one_node_before_changing_current(tmp_path):
    result, calls, current, state, releases = _canary_autodeploy(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "deploy --stage-only v4.3.0" in calls
    assert current == "v4.2.0"
    assert state == {"target": "v4.3.0", "node": "canary", "previous": "v4.2.0", "started_at": ""}
    assert "deploy v4.3.0" not in calls


def test_autodeploy_rolls_back_only_the_canary_when_fake_registry_rate_exceeds_baseline(tmp_path):
    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=21)
    runs = [{"node": "stable", "started_at": started - timedelta(hours=1) + timedelta(minutes=i),
             "status": "finished", "outcome": "failed" if i == 0 else "done",
             "error_kind": "provider" if i == 0 else ""} for i in range(10)]
    runs += [{"node": "canary", "started_at": started + timedelta(minutes=i), "status": "finished",
              "outcome": outcome, "error_kind": error_kind}
             for i, (outcome, error_kind) in enumerate((("failed", "worker"), ("blocked", "infra"),
                                                        ("lost", ""), ("quota", "quota"), ("done", "")))]
    registry = _FakeCanaryRegistry({"stable": "v4.2.0", "canary": "v4.3.0"}, runs)
    measurements = measure_registry(registry, "canary", started, "v4.2.0")
    decision = decide_canary(measurements, 21 * 60, Thresholds(), installed=True)

    result, calls, current, state, releases = _canary_autodeploy(tmp_path, active=True, decision=decision, now=started)

    assert result.returncode == 0, result.stdout + result.stderr
    assert decision["action"] == "rollback"
    assert current == "v4.2.0"
    assert state is None
    assert (releases / "CANARY_REJECTED").read_text().strip() == "v4.3.0"
    assert "canary record-rollback" in calls
    assert "abcdef0123456789" in calls
    assert "deploy v4.3.0" not in calls


def test_autodeploy_promotes_stable_canary_and_records_under_observed_timeout(tmp_path):
    now = datetime.now(timezone.utc)
    started = now - timedelta(hours=1, seconds=1)
    metrics = {"canary": {"runs": 2, "infra_failures": 0, "failure_rate": 0.0},
               "baseline": {"runs": 10, "infra_failures": 0, "failure_rate": 0.0}}
    decision = decide_canary(metrics, 3601, Thresholds(), installed=True)

    result, calls, current, state, _ = _canary_autodeploy(tmp_path, active=True, decision=decision, now=started)

    assert result.returncode == 0, result.stdout + result.stderr
    assert decision["action"] == "promote"
    assert "deploy v4.3.0" in calls
    assert "record-promotion" in calls
    assert current == "v4.3.0"
    assert state is None

