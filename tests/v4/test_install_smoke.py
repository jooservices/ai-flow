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


def test_install_and_operations_docs_describe_the_release_gate():
    install = (ROOT / "docs/v4/install.md").read_text()
    operations = (ROOT / "docs/v4/node-operations.md").read_text()
    deploy_readme = (ROOT / "deploy/v4/README.md").read_text()
    assert "before AI Flow publishes" in install
    assert "Ubuntu 24.04" in install and "two nodes" in install
    assert "upgrade" in install and "rollback" in install
    assert "failed step blocks publication" in operations
    assert "docs/v4/install.md" in deploy_readme
