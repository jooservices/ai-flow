"""Upgrade layout and promotion failure alerts (JAIF-344, docs/v4/upgrade-layout.md).

An older installation may lack files a fresh install creates (runtime.env). Upgrades and promotions must
create them before services start; a failed promotion must raise a CRITICAL alert that names the step,
error, stuck build and backlog, and must stop retrying the same build after two failures with the same cause.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from aiflow.v4.core.settings import read_env_file
from aiflow.v4.control import runtime_settings
from aiflow.v4.install import promotion
from aiflow.v4.install.secrets import ensure_runtime_layout

from tests.v4.test_clean_install_acceptance import MockControl
from tests.v4.test_control_install import FakeHost, installer

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def machine(tmp_path):
    root = tmp_path / "host"
    (root / "etc").mkdir(parents=True)
    (root / "etc/os-release").write_text('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n')
    (root / "usr/local/bin").mkdir(parents=True)
    (root / "usr/local/bin/cosign").write_bytes(b"cosign")
    return FakeHost(root)


def test_ensure_runtime_layout_creates_missing_runtime_env_with_safe_permissions(tmp_path):
    secrets = tmp_path / "etc" / "aiflow" / "secrets"
    secrets.mkdir(parents=True)
    # Previous-release layout: control secrets may exist; runtime.env does not.
    (secrets / "control").mkdir()
    assert not (secrets / "runtime.env").exists()

    created = ensure_runtime_layout(secrets)

    runtime = secrets / "runtime.env"
    assert "runtime.env" in created
    assert runtime.is_file()
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o600
    assert "Owner-managed runtime secrets" in runtime.read_text(encoding="utf-8")
    assert (tmp_path / "etc" / "aiflow" / "projects").is_dir()
    # A second call keeps the file and reports nothing new for runtime.env.
    runtime.write_text("# Owner-managed runtime secrets (Command Center)\nKEPT=1\n", encoding="utf-8")
    assert "runtime.env" not in ensure_runtime_layout(secrets)
    assert "KEPT=1" in runtime.read_text(encoding="utf-8")


def test_optional_runtime_env_absent_is_treated_as_empty_by_services(tmp_path):
    missing = tmp_path / "missing-runtime.env"
    assert read_env_file(missing) == {}
    assert runtime_settings._file_rows(missing, "secret") == []


def test_upgrade_from_previous_release_layout_recreates_runtime_env(machine):
    assert installer(machine, [], version="v4.1.0").run() == 0
    runtime = machine.p("/etc/aiflow/secrets/runtime.env")
    assert runtime.is_file()
    runtime.unlink()  # simulate an installation that predates JAIF-179
    assert not runtime.exists()

    out: list[str] = []
    assert installer(machine, out, version="v4.2.0").run() == 0
    assert runtime.is_file()
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o600
    assert machine.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.2.0"


def test_normalize_cause_extracts_missing_file_path():
    assert promotion.normalize_cause(
        "env file /etc/aiflow/secrets/runtime.env not found") == "missing file /etc/aiflow/secrets/runtime.env"
    assert promotion.normalize_cause("something else failed") == "something else failed"


def test_record_failure_exhausts_after_two_same_cause(tmp_path):
    path = tmp_path / promotion.FAILURES_NAME
    first = promotion.record_failure(path, build="abc", step="deploy",
                                     error="env file /etc/aiflow/secrets/runtime.env not found")
    assert first["count"] == 1 and first["exhausted"] is False
    second = promotion.record_failure(path, build="abc", step="deploy",
                                      error="open /etc/aiflow/secrets/runtime.env: no such file")
    assert second["count"] == 2 and second["exhausted"] is True
    assert second["cause"] == "missing file /etc/aiflow/secrets/runtime.env"
    # A different cause resets the strike count for that build.
    other = promotion.record_failure(path, build="abc", step="health", error="control-api-a is unhealthy")
    assert other["count"] == 1 and other["exhausted"] is False


def test_should_try_build_blocks_rejected_and_waits_for_missing_file_cause(tmp_path):
    missing = tmp_path / "runtime.env"
    failures = {"build": "old", "cause": f"missing file {missing}", "count": 2, "exhausted": True}
    ok, reason = promotion.should_try_build(rejected="old", target="old", failures=failures)
    assert ok is False and "already failed" in reason
    ok, reason = promotion.should_try_build(rejected="old", target="new", failures=failures)
    assert ok is False and "waiting for cause" in reason
    missing.write_text("# ready\n", encoding="utf-8")
    ok, reason = promotion.should_try_build(rejected="old", target="new", failures=failures)
    assert ok is True and reason == ""


def test_alert_detail_names_step_error_stuck_build_and_backlog():
    detail = promotion.alert_detail(
        build="deadbeef01", step="deploy",
        error="env file /etc/aiflow/secrets/runtime.env not found",
        not_live=7, previous="cafebabe00", count=2, exhausted=True)
    assert "Failed step: deploy" in detail
    assert "missing file /etc/aiflow/secrets/runtime.env" in detail
    assert "Stuck build: deadbeef01" in detail
    assert "Merged changes not live: 7" in detail
    assert "Stopped retrying deadbeef01" in detail


def _run_failed_promotion(root: Path, script: str, env_file: Path, control: MockControl,
                          prior_state: dict | None = None):
    """Like run_promotion, but keeps one releases directory so strike counts can accumulate."""
    releases = root / "control/releases"
    releases.mkdir(parents=True, exist_ok=True)
    stubs = root / "stubs"
    stubs.mkdir(exist_ok=True)
    calls = root / "calls.log"
    if calls.exists():
        calls.unlink()
    (releases / "CURRENT").write_text("v4.1.0")
    (releases / "CANARY.json").write_text(json.dumps({
        "target": "v4.2.0", "node": "node1", "previous": "v4.1.0",
        "started_at": "2026-01-01T00:00:00+00:00", "requested_at": "2026-01-01T00:00:00+00:00"}))
    if prior_state is not None:
        (releases / promotion.FAILURES_NAME).write_text(json.dumps(prior_state) + "\n", encoding="utf-8")
    for name, body in {"docker": 'case "$*" in *evaluate*) echo \'{"action":"promote","reason":"stable"}\';; *) echo "{}";; esac',
                       "sleep": "", "aiflow4-deploy": "", "aiflow4-deploy-develop": ""}.items():
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\n{body}\n')
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "AIFLOW4_ROOT": str(root),
           "AIFLOW4_CONTROL_URL": control.url, "AIFLOW4_CONTROL_ENV": str(env_file),
           "AIFLOW4_SECRETS_DIR": str(root / "secrets")}
    (root / "secrets").mkdir(exist_ok=True)
    done = subprocess.run(["bash", str(ROOT / "deploy/v4/bin" / script)], env=env, capture_output=True,
                          text=True, timeout=120)
    return done, calls.read_text().splitlines() if calls.exists() else [], releases


@pytest.mark.parametrize("script,deploy", [("aiflow4-autodeploy", "aiflow4-deploy"),
                                           ("aiflow4-autodeploy-develop", "aiflow4-deploy-develop")])
def test_failed_promotion_alert_records_cause_and_rejects_after_two_strikes(tmp_path, script, deploy):
    env_file = tmp_path / "owner.env"
    env_file.write_text("AIFLOW4_OWNER_TOKEN=not-the-owner-token\n")
    control = MockControl("owner-token")
    try:
        first, calls1, releases = _run_failed_promotion(tmp_path / "promo", script, env_file, control)
        assert first.returncode == 1, first.stdout + first.stderr
        assert "unhealthy; rolling back" in first.stdout
        notification = next(call for call in calls1 if "aiflow4 notify" in call)
        assert "--severity critical" in notification
        assert "[CRITICAL] AI Flow:" in notification and "promotion of v4.2.0 failed" in notification
        assert all(label in notification for label in (
            "Failed step: deploy.", "Error:", "Stuck build: v4.2.0", "Merged changes not live:"))
        state = json.loads((releases / promotion.FAILURES_NAME).read_text(encoding="utf-8"))
        assert state["count"] == 1 and state["exhausted"] is False
        assert state["build"] == "v4.2.0"
        assert "Failed step" in state.get("error", "") or state["cause"]

        second, calls2, releases = _run_failed_promotion(
            tmp_path / "promo", script, env_file, control, prior_state=state)
        assert second.returncode == 1, second.stdout + second.stderr
        final = json.loads((releases / promotion.FAILURES_NAME).read_text(encoding="utf-8"))
        assert final["exhausted"] is True and final["count"] == 2
        assert (releases / "CANARY_REJECTED").read_text().strip() == "v4.2.0"
        assert [c for c in calls2 if c.startswith(f"{deploy} ")][-1].endswith("v4.1.0")
    finally:
        control.close()


def test_deploy_scripts_call_ensure_runtime_layout_before_compose():
    for name in ("aiflow4-deploy", "aiflow4-deploy-develop"):
        text = (ROOT / "deploy/v4/bin" / name).read_text(encoding="utf-8")
        assert "ensure_runtime_layout" in text
        assert text.index("ensure_runtime_layout") < text.index("docker compose -f")


def test_docs_describe_upgrade_layout_and_topic_is_linked():
    doc = (ROOT / "docs/v4/upgrade-layout.md").read_text(encoding="utf-8")
    assert "ensure_runtime_layout" in doc
    assert "two" in doc.lower() and "same cause" in doc.lower()
    readme = (ROOT / "deploy/v4/README.md").read_text(encoding="utf-8")
    assert "[Upgrade host layout and promotion failures](../../docs/v4/upgrade-layout.md)" in readme
    topic = (ROOT / "deploy/v4/topics/upgrade-layout.md").read_text(encoding="utf-8").strip()
    assert topic.startswith("- [") and "upgrade-layout.md" in topic
