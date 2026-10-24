import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_release_publication_waits_for_install_smokes_and_security_scan():
    workflow = yaml.load((ROOT / ".github/workflows/release-v4.yml").read_text(), Loader=yaml.BaseLoader)
    release = workflow["jobs"]["release"]
    assert release["needs"] == ["v4-tests", "acceptance"]
    names = [step.get("name", "") for step in release["steps"]]
    clean_install = names.index("Clean-install signed candidate on Ubuntu control and two nodes")
    rollout_smoke = names.index("Fleet rollout and rollback smoke using signed candidate artifacts")
    build = names.index("Build control-api image locally")
    scan = names.index("Security scan and release security report")
    publish_image = names.index("Publish control-api image to GHCR")
    sign_image = names.index("Sign control-api image with GitHub OIDC")
    publish = names.index("Create GitHub Release and upload assets")
    assert clean_install < rollout_smoke < build < scan < publish_image < sign_image < publish
    assert "clean-install-containers.sh" in release["steps"][clean_install]["run"]
    assert "install-smoke.sh" in release["steps"][rollout_smoke]["run"]
    assert "security-scan.sh" in release["steps"][scan]["run"]
    assert "SECURITY_SEVERITY_THRESHOLD" in release["steps"][scan]["env"]
    assert "dist/SECURITY-REPORT.md" in release["steps"][publish]["run"]
    validate = next(step for step in release["steps"] if step.get("name") == "Validate tag and master ancestry")
    assert "gh release list" in validate["run"]
    assert "git merge-base --is-ancestor" in validate["run"]
    assert "git describe" not in validate["run"]
    build = next(step for step in release["steps"] if step.get("name") == "Build tarball and conventional changelog")
    assert build["env"]["PREVIOUS"] == "${{ steps.release.outputs.previous }}"


def test_install_smoke_uses_signed_artifact_and_two_systemd_nodes_for_heartbeat_and_claim():
    script = (ROOT / "deploy/v4/ci/install-smoke.sh").read_text()
    dockerfile = (ROOT / "deploy/v4/ci/systemd.Dockerfile").read_text()
    assert "FROM ubuntu:24.04" in dockerfile
    assert "systemd" in dockerfile
    assert "cosign verify" in (ROOT / "deploy/v4/bin/aiflow4-node-install").read_text()
    assert 'start_systemd "$CONTROL" control' in script
    assert 'start_systemd "$NODE_A" smoke-node-a' in script
    assert 'start_systemd "$NODE_B" smoke-node-b' in script
    assert "/v1/nodes/join-tokens" in script
    assert "/v1/work/claim" in script
    assert "wait_for_release smoke-node-a" in script
    assert "wait_for_release smoke-node-b" in script
    assert "node claim smoke passed" in script


def test_console_browser_suite_runs_in_pr_and_release_smokes_with_failure_traces():
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    v4 = ci["jobs"]["v4"]
    install = next(step for step in v4["steps"] if step.get("name") == "Install")
    assert "npm install --global playwright@1.64.1" in install["run"]
    assert "playwright install chromium --with-deps" in install["run"]
    assert "tests/v4" in next(step for step in v4["steps"] if step.get("name") == "Tests (real Redis and PostgreSQL)")["run"]
    upload = next(step for step in v4["steps"] if step.get("name") == "Upload sanitized Playwright traces")
    assert upload["if"] == "failure()"
    assert "aiflow-playwright-traces" in upload["with"]["path"]

    release_workflow = yaml.load((ROOT / ".github/workflows/release-v4.yml").read_text(), Loader=yaml.BaseLoader)
    release_tests = release_workflow["jobs"]["v4-tests"]
    release_install = next(step for step in release_tests["steps"] if step.get("name") == "Install")
    assert "playwright install chromium --with-deps" in release_install["run"]
    release_job = release_workflow["jobs"]["release"]
    smoke = next(step for step in release_job["steps"]
                 if step.get("name") == "Fleet rollout and rollback smoke using signed candidate artifacts")
    assert smoke["env"]["AIFLOW4_TEST_REDIS_URL"]
    assert smoke["env"]["AIFLOW4_TEST_DATABASE_URL"]
    assert "aiflow-playwright-traces" in smoke["env"]["AIFLOW4_PLAYWRIGHT_TRACE_DIR"]
    trace_upload = next(step for step in release_job["steps"]
                        if step.get("name") == "Upload sanitized release install smoke Playwright traces")
    assert trace_upload["if"] == "failure()"
    assert "aiflow-playwright-traces" in trace_upload["with"]["path"]

    smoke_script = (ROOT / "deploy/v4/ci/install-smoke.sh").read_text()
    assert "tests/v4/test_console_browser.py" in smoke_script
    assert '"${AIFLOW4_TEST_DATABASE_URL:?AIFLOW4_TEST_DATABASE_URL must point' in smoke_script


def test_install_smoke_checks_upgrade_and_failed_candidate_rollback_then_finishes_install():
    script = (ROOT / "deploy/v4/ci/install-smoke.sh").read_text()
    assert "AIFLOW4_SELF_UPDATE=1" in script
    assert "candidate-started" in script
    assert "rollback installer returned" in script
    installer = (ROOT / "deploy/v4/bin/aiflow4-node-install").read_text()
    assert "systemctl restart aiflow4-node-agent || true" in installer
    assert "systemctl reset-failed aiflow4-node-agent || true" in installer
    assert "wait_for_release smoke-node-a \"$VERSION\"" in script
    assert 'wait_for_release smoke-node-b "$PREVIOUS_VERSION"' in script
    assert '"/artifacts/aiflow4-$VERSION.tar.gz.sigstore.json" "$CHECKSUM"' in script
    assert 'wait_for_release smoke-node-b "$VERSION"' in script


def _state_checks(script: str) -> tuple[str, dict[str, str]]:
    program = re.search(r"^STATE_CHECK='(.*?)'$", script, re.S | re.M).group(1)
    checks = dict(re.findall(r'wait_for_state "([^"]+)"\s*\\?\s*\'(.*?)\'', script, re.S))
    return program, checks


def _check(program: str, expression: str, state: dict) -> bool:
    result = subprocess.run([sys.executable, "-c", program, expression, "v1.3.0", "v1.2.0"],
                            input=json.dumps(state), text=True, capture_output=True)
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


def test_install_smoke_asserts_the_failure_report_halts_the_rollout_and_the_fleet_rolls_back():
    script = (ROOT / "deploy/v4/ci/install-smoke.sh").read_text()
    # control-loops drives canary, waves and halt; without it a tagged previous release would never be upgraded
    assert 'unit control-loops "control loops" "control-loops"' in script
    loops_start = script.index('docker exec "$CONTROL" systemctl enable --now aiflow4-control-loops.service')
    assert script.count('docker exec "$CONTROL" systemctl enable --now aiflow4-control-loops.service') == 1
    assert script.index('bootstrap_nodes "$NODE_B" smoke-node-b smoke-b') < loops_start
    assert script.index('wait_for_release smoke-node-a "$PREVIOUS_VERSION"') < loops_start
    assert script.index('wait_for_release smoke-node-b "$PREVIOUS_VERSION"') < loops_start
    assert "AIFLOW4_ROLLOUT_CANARY_NODE=smoke-node-a" in script
    assert '[[ $BASELINE -eq 1 ]] || publish "$PREVIOUS_VERSION"' in script
    assert 'set_current "$PREVIOUS_VERSION"' in script
    program, checks = _state_checks(script)
    incident = checks["the release_verification incident for node B"]
    halted = checks["the rollout of $VERSION halted by node B"]
    rolled_back = checks["the fleet rollback of $VERSION to succeed"]
    wave = checks["node A healthy and node B assigned $VERSION in wave 1"]
    # the report check precedes the halt check, which precedes the canary's return to the previous release
    assert script.index(incident) < script.index(halted) < script.index(rolled_back)
    assert 'wait_for_release smoke-node-a "$PREVIOUS_VERSION"' in script[script.index(halted):script.index(rolled_back)]

    rolling = {"rollout": {"target": "v1.3.0", "status": "rolling", "failed_node": None, "reason": "",
                           "rollback": None, "nodes": [{"node": "smoke-node-a", "status": "healthy"},
                                                       {"node": "smoke-node-b", "status": "assigned"}]},
               "incidents": []}
    assert _check(program, wave, rolling)
    assert not _check(program, incident, rolling)
    assert not _check(program, halted, rolling)

    reported = {**rolling, "incidents": [{"kind": "release_verification", "subject": "smoke-node-b:v1.3.0"}]}
    assert _check(program, incident, reported)
    assert not _check(program, halted, reported)  # recorded but not halted: the smoke must fail

    halted_state = {
        "rollout": {**rolling["rollout"], "status": "halted", "failed_node": "smoke-node-b", "rollback": "pending",
                    "reason": "rejected the release: installer: agent unhealthy on v1.3.0; switched back to v1.2.0",
                    "nodes": [{"node": "smoke-node-a", "status": "rolling_back"},
                              {"node": "smoke-node-b", "status": "rolled_back"}]},
        "incidents": reported["incidents"] + [{"kind": "fleet_rollout", "subject": "v1.3.0"}]}
    assert _check(program, halted, halted_state)
    assert not _check(program, rolled_back, halted_state)
    timed_out = {**halted_state, "rollout": {**halted_state["rollout"], "failed_node": "smoke-node-b",
                                             "reason": "lost its heartbeat (90 s ago)"}}
    assert not _check(program, halted, timed_out)  # only a halt caused by the installer's report counts

    recovered = {"rollout": {**halted_state["rollout"], "rollback": "succeeded",
                             "nodes": [{"node": "smoke-node-a", "status": "rolled_back"},
                                       {"node": "smoke-node-b", "status": "rolled_back"}]},
                 "incidents": halted_state["incidents"] + [{"kind": "fleet_rollback", "subject": "v1.3.0"}]}
    assert _check(program, rolled_back, recovered)


def test_three_argument_install_smoke_setup_uses_baseline_artifact_and_parent_version(tmp_path):
    helper = ROOT / "deploy/v4/ci/install-smoke-artifacts.sh"
    tools = tmp_path / "bin"
    tools.mkdir()
    git_stub = tools / "git"
    git_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ $1 == -C ]]; then shift 2; fi\n"
        "case $1 in\n"
        "  rev-parse) printf '%s\\n' \"$PARENT_COMMIT\" ;;\n"
        "  archive)\n"
        "    output=\n"
        "    for argument in \"$@\"; do\n"
        "      [[ $argument == --output=* ]] && output=${argument#--output=}\n"
        "    done\n"
        "    printf '%s\\n' \"$*\" > \"$GIT_ARCHIVE_LOG\"\n"
        "    printf 'baseline archive stub' > \"$output\"\n"
        "    ;;\n"
        "  *) echo \"unexpected git command: $*\" >&2; exit 1 ;;\n"
        "esac\n"
    )
    git_stub.chmod(0o755)

    candidate = tmp_path / "candidate.tar.gz"
    bundle = tmp_path / "candidate.sigstore.json"
    candidate.write_bytes(b"candidate artifact")
    bundle.write_bytes(b"candidate signature")
    artifacts = tmp_path / "artifacts"
    parent = "0123456789abcdef0123456789abcdef01234567"
    archive_log = tmp_path / "archive-args.txt"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tools}{os.pathsep}{env['PATH']}",
            "GITHUB_SHA": "fedcba9876543210fedcba9876543210fedcba98",
            "PARENT_COMMIT": parent,
            "GIT_ARCHIVE_LOG": str(archive_log),
        }
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; shift; prepare_install_smoke_artifacts "$@"; '
            'printf "%s\\n%s\\n%s\\n%s\\n" "$VERSION" "$BASELINE" '
            '"$PREVIOUS_VERSION" "$PREVIOUS_TARBALL"',
            "test-install-smoke-artifacts",
            str(helper),
            str(ROOT),
            str(artifacts),
            "v1.2.3",
            str(candidate),
            str(bundle),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    version, baseline, previous_version, previous_tarball = result.stdout.splitlines()
    expected_baseline = artifacts / f"aiflow4-baseline-{parent}.tar.gz"
    assert version == "v1.2.3"
    assert baseline == "1"
    assert previous_version == parent
    assert previous_tarball == str(expected_baseline)
    assert expected_baseline.read_bytes() == b"baseline archive stub"
    assert not (artifacts / f"aiflow4-{parent}.tar.gz").exists()
    assert f"{parent} aiflow pyproject.toml uv.lock prompts deploy" in archive_log.read_text()

    script = (ROOT / "deploy/v4/ci/install-smoke.sh").read_text()
    assert "if [[ $BASELINE -eq 0 ]]; then" in script
    assert 'docker exec --env BASELINE="$baseline"' in script


def test_control_install_smoke_covers_install_rerun_upgrade_and_rollback_on_ubuntu_systemd():
    script = (ROOT / "deploy/v4/ci/control-install-smoke.sh").read_text()
    assert 'docker build --pull -f "$ROOT/deploy/v4/ci/systemd.Dockerfile"' in script
    assert "FROM ubuntu:24.04" in (ROOT / "deploy/v4/ci/systemd.Dockerfile").read_text()
    steps = ["== 1. clean install of $PREVIOUS", "== 2. the same command again",
             "== 3. failed upgrade to $TARGET rolls back", "== 4. upgrade to $TARGET", "== 5. owner rollback"]
    assert [script.index(s) for s in steps] == sorted(script.index(s) for s in steps)
    assert "((ELAPSED <= 600))" in script  # Command Center within 10 minutes
    assert '[[ $BEFORE == "$AFTER" ]]' in script  # the second run changes nothing
    assert "AIFLOW4_INSTALL_SIMULATE_UNHEALTHY=control-api-a" in script and "[[ $RC == 5 ]]" in script
    assert "install control --rollback" in script
    # the fingerprint compares hashes and ids; secret values never leave the container
    fingerprint = script[script.index("fingerprint() {"):script.index("current() {")]
    assert "sha256sum /etc/aiflow/secrets/control/*.env" in fingerprint and "cat /etc/aiflow" not in fingerprint


def test_install_and_operations_docs_describe_the_release_gate():
    install = (ROOT / "docs/v4/install.md").read_text()
    operations = (ROOT / "docs/v4/node-operations.md").read_text()
    deploy_readme = (ROOT / "deploy/v4/README.md").read_text()
    assert "before AI Flow publishes" in install
    assert "Ubuntu 24.04" in install and "two nodes" in install
    assert "upgrade" in install and "rollback" in install
    assert "release_verification" in install and "halted" in install and "fleet_rollback" in install
    assert "failed step blocks publication" in operations
    assert "release_verification" in operations and "halted rollout" in operations
    assert "docs/v4/install.md" in deploy_readme
