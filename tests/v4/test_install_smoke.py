import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_release_publication_waits_for_systemd_install_smoke():
    workflow = yaml.load((ROOT / ".github/workflows/release-v4.yml").read_text(), Loader=yaml.BaseLoader)
    release = workflow["jobs"]["release"]
    assert release["needs"] == "v4-tests"
    names = [step.get("name", "") for step in release["steps"]]
    smoke = names.index("Install release on Ubuntu systemd control and two nodes")
    image = names.index("Build and push control-api image to GHCR")
    publish = names.index("Create GitHub Release and upload assets")
    assert smoke < image < publish
    assert "install-smoke.sh" in release["steps"][smoke]["run"]
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


def test_install_and_operations_docs_describe_the_release_gate():
    install = (ROOT / "docs/v4/install.md").read_text()
    operations = (ROOT / "docs/v4/node-operations.md").read_text()
    deploy_readme = (ROOT / "deploy/v4/README.md").read_text()
    assert "before AI Flow publishes" in install
    assert "Ubuntu 24.04" in install and "two nodes" in install
    assert "upgrade" in install and "rollback" in install
    assert "failed step blocks publication" in operations
    assert "docs/v4/install.md" in deploy_readme
