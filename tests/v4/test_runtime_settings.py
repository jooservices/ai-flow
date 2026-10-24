"""Owner-only runtime env settings: masked responses, drained restarts, and rollback on failed health checks."""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.core.controlrepo import Account, Node
from aiflow.v4.core.runtime_settings_crypto import node_public_key
from aiflow.v4.core.settings import read_env_file
from aiflow.v4.node import agent as node_agent
from aiflow.v4.node import operations
from aiflow.v4.control.ui import mount_ui
from aiflow.v4.control import runtime_settings
from aiflow.v4.install.secrets import update_env

SESSION_SECRET = "runtime-settings-test-session-secret-32-bytes"
OWNER_COOKIE = "aiflow4_session"


def sign_in(client, ctx, role="owner"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set(OWNER_COOKIE, issue_session("owner@example.test", role, 2_000_000_000, SESSION_SECRET))


def join_and_heartbeat(client, name, role="worker", project="TESTWP", settings_dir=None):
    joined = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": name,
                                                         "role": role, "project": project})
    assert joined.status_code == 200
    enrolled = client.post("/v1/nodes/join", json={"join_token": joined.json()["token"]})
    assert enrolled.status_code == 200
    token = enrolled.json()["node_token"]
    project_settings = node_agent.runtime_settings_inventory(settings_dir or "/missing-project-env")
    heartbeat = client.post(f"/v1/nodes/{name}/heartbeat", headers={"Authorization": f"Bearer {token}"}, json={
        "slots_total": 2, "slots_used": 0, "release_version": "v4.0.0", "release_protocol": 1,
        "tools": {"git": "git version 2.43", "python3": "Python 3.12"},
        "runtime_settings": project_settings, "runtime_settings_public_key": node_public_key(),
    })
    assert heartbeat.status_code == 200, heartbeat.text
    return token


def next_operation(client, node, token):
    response = client.get(f"/v1/nodes/{node}/operations/next", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()["operation"]


def finish_operation(client, node, token, operation, result, status="completed"):
    response = client.post(f"/v1/nodes/{node}/operations/{operation['id']}/result",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"status": status, "result": result})
    assert response.status_code == 200, response.text
    return response


def checkpoint_operation(client, node, token, operation, envelope):
    response = client.post(f"/v1/nodes/{node}/operations/{operation['id']}/checkpoint",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"rollback_envelope": envelope})
    assert response.status_code == 200, response.text
    return response.json()["rollback_envelope"]


def test_owner_lists_and_sets_control_secrets_without_returning_or_auditing_values(
        client, ctx, tmp_path, monkeypatch, caplog):
    mount_ui(client.app, ctx)
    settings_dir = tmp_path / "projects"
    settings_dir.mkdir()
    (settings_dir / "TESTWP.env").write_text("FEATURE_FLAG=enabled\n")
    secret_file = tmp_path / "secrets" / "runtime.env"
    secret_file.parent.mkdir()
    secret_file.write_text("EXISTING_API_TOKEN=$EXISTING_NAME\n")
    ctx.settings.runtime_secret_file = str(secret_file)
    ctx.settings.project_env_dir = str(settings_dir)
    control_token = join_and_heartbeat(client, "control", role="control", settings_dir=settings_dir)
    sign_in(client, ctx)

    page = client.get("/settings")
    assert page.status_code == 200 and "Runtime settings" in page.text
    listing = client.get("/v1/runtime-settings")
    assert listing.status_code == 200
    inventory = listing.json()
    listed = {item["name"]: item for item in inventory["settings"]}
    assert {"EXISTING_API_TOKEN", "FEATURE_FLAG"} <= set(listed)
    assert listed["EXISTING_API_TOKEN"]["is_set"] is True
    assert listed["FEATURE_FLAG"]["is_set"] is True
    assert "$EXISTING_NAME" not in listing.text and "enabled" not in listing.text

    invalid_secret = secrets.token_urlsafe(24)
    invalid = client.post("/v1/runtime-settings", json={"scope": "secret", "key": "REJECTED_TOKEN",
                                                         "value": invalid_secret * 200})
    assert invalid.status_code == 422 and invalid_secret not in invalid.text

    secret_value = "$NAME"
    monkeypatch.setattr(operations, "RUNTIME_SECRET_FILE", secret_file)
    monkeypatch.setattr(operations, "_control_compose_file", lambda: (tmp_path / "compose.yaml", {}))
    restarted = []
    monkeypatch.setattr(operations, "_control_api_healthy", lambda service, _compose, _env:
                        restarted.append(service) is None or True)
    saved = client.post("/v1/runtime-settings", json={"scope": "secret", "key": "EXTERNAL_API_TOKEN",
                                                       "value": secret_value})
    assert saved.status_code == 202, saved.text
    assert secret_value not in saved.text
    assert saved.json()["services"] == ["control-api-a", "control-api-b"]

    queued = ctx.db.one("SELECT payload FROM node_operations WHERE id=%s", (saved.json()["operations"][0]["id"],))
    serialized_operation = json.dumps(queued["payload"])
    assert secret_value not in serialized_operation
    operation = next_operation(client, "control", control_token)
    assert secret_value not in json.dumps(operation)
    result = operations.execute(operation)
    finish_operation(client, "control", control_token, operation, result)
    assert "EXISTING_API_TOKEN='$EXISTING_NAME'" in secret_file.read_text()
    assert "EXTERNAL_API_TOKEN='$NAME'" in secret_file.read_text()
    assert read_env_file(secret_file)["EXTERNAL_API_TOKEN"] == secret_value
    assert read_env_file(secret_file)["EXISTING_API_TOKEN"] == "$EXISTING_NAME"
    assert restarted == ["control-api-a", "control-api-b"]

    change = client.get(f"/v1/runtime-settings/changes/{saved.json()['change_id']}")
    refreshed = client.get("/v1/runtime-settings")
    assert change.status_code == refreshed.status_code == 200
    assert secret_value not in change.text + refreshed.text + caplog.text
    audit = ctx.db.one("SELECT actor,ticket,payload,result FROM write_audit WHERE target='runtime-settings'"
                       " AND action='set' ORDER BY id DESC LIMIT 1")
    assert audit["actor"] == "owner@example.test" and audit["ticket"] == "EXTERNAL_API_TOKEN"
    assert audit["payload"]["key"] == "EXTERNAL_API_TOKEN" and audit["payload"]["set"] is True
    assert secret_value not in json.dumps(audit)


def test_project_setting_drains_node_restarts_worker_and_restores_acceptance(
        client, ctx, tmp_path, monkeypatch):
    project_dir = tmp_path / "project-env"
    project_dir.mkdir()
    project_file = project_dir / "TESTWP.env"
    project_file.write_text("FEATURE_MODE=old-mode\n")
    control_dir = tmp_path / "control-projects"
    control_dir.mkdir()
    (control_dir / "TESTWP.env").write_text("FEATURE_MODE=old-mode\n")
    ctx.settings.project_env_dir = str(control_dir)
    control_token = join_and_heartbeat(client, "control", role="control", settings_dir=control_dir)
    token = join_and_heartbeat(client, "node1", settings_dir=project_dir)
    sign_in(client, ctx)
    services = []
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", project_dir)
    monkeypatch.setattr(operations, "_wait_for_worker_idle", lambda units: True)
    monkeypatch.setattr(operations, "_restart_worker", lambda unit: services.append(unit) is None or True)

    quoted = client.post("/v1/runtime-settings", json={"scope": "project", "project": "TESTWP",
                                                        "key": "FEATURE_MODE", "value": '"wrapped-mode"'})
    assert quoted.status_code == 422
    assert read_env_file(project_file)["FEATURE_MODE"] == "old-mode"

    value = "new-mode"
    response = client.post("/v1/runtime-settings", json={"scope": "project", "project": "TESTWP",
                                                          "key": "FEATURE_MODE", "value": value})
    assert response.status_code == 202, response.text
    assert "node1: aiflow4-worker@codex-01.service" in response.json()["services"]
    node = ctx.db.one("SELECT accepting_work,status FROM nodes WHERE name='node1'")
    assert node["accepting_work"] is False and node["status"] == "draining"

    operation = next_operation(client, "node1", token)
    assert value not in json.dumps(operation)
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", project_dir)
    result = operations.execute(operation)
    finish_operation(client, "node1", token, operation, result)
    assert read_env_file(project_file)["FEATURE_MODE"] == value
    assert services == ["aiflow4-worker@codex-01.service"]
    assert ctx.db.one("SELECT accepting_work,status FROM nodes WHERE name='node1'") == {
        "accepting_work": False, "status": "draining"}
    central_operation = next_operation(client, "control", control_token)
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", control_dir)
    central_result = operations.execute(central_operation)
    finish_operation(client, "control", control_token, central_operation, central_result)
    assert read_env_file(control_dir / "TESTWP.env")["FEATURE_MODE"] == value
    node = ctx.db.one("SELECT accepting_work,status FROM nodes WHERE name='node1'")
    assert node["accepting_work"] is True and node["status"] == "online"
    status = client.get(f"/v1/runtime-settings/changes/{response.json()['change_id']}")
    assert status.json()["status"] == "completed"
    assert value not in status.text


def test_only_authenticated_owner_can_open_or_change_runtime_settings(client, ctx):
    mount_ui(client.app, ctx)
    ctx.settings.console_session_secret = SESSION_SECRET
    assert client.get("/settings").status_code == 401
    assert client.get("/v1/runtime-settings").status_code == 401
    assert client.post("/v1/runtime-settings", json={"scope": "secret", "key": "TOKEN",
                                                      "value": "sample-value"}).status_code == 401

    sign_in(client, ctx, role="business")
    assert client.get("/settings").status_code == 403
    assert client.get("/v1/runtime-settings").status_code == 403
    refused = client.post("/v1/runtime-settings", json={"scope": "secret", "key": "TOKEN",
                                                         "value": "sample-value"})
    assert refused.status_code == 403
    assert ctx.db.one("SELECT count(*) AS n FROM node_operations WHERE action LIKE 'runtime-setting%%'")["n"] == 0
    assert not ctx.db.one("SELECT 1 AS written FROM write_audit WHERE target='runtime-settings' AND accepted=true")


def test_failed_worker_health_check_reports_service_and_restores_previous_value(client, ctx, tmp_path, monkeypatch):
    project_dir = tmp_path / "project-env"
    project_dir.mkdir()
    project_file = project_dir / "TESTWP.env"
    previous = "old-runtime-value"
    project_file.write_text(f"SERVICE_TOKEN={previous}\n")
    control_dir = tmp_path / "control-projects"
    control_dir.mkdir()
    (control_dir / "TESTWP.env").write_text("SERVICE_TOKEN=old-central-value\n")
    ctx.settings.project_env_dir = str(control_dir)
    join_and_heartbeat(client, "control", role="control", settings_dir=control_dir)
    token = join_and_heartbeat(client, "node1")
    sign_in(client, ctx)
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", project_dir)
    monkeypatch.setattr(operations, "_wait_for_worker_idle", lambda _units: True)
    restarted = []
    def restart_once_then_rollback(unit):
        restarted.append(unit)
        return len(restarted) > 1

    monkeypatch.setattr(operations, "_restart_worker", restart_once_then_rollback)

    replacement = secrets.token_urlsafe(18)
    saved = client.post("/v1/runtime-settings", json={"scope": "project", "project": "TESTWP",
                                                       "key": "SERVICE_TOKEN", "value": replacement})
    assert saved.status_code == 202
    operation = next_operation(client, "node1", token)
    try:
        operations.execute(operation)
    except operations.OperationError as error:
        failure = {"healthy": False, "error": str(error), "failed_service": "aiflow4-worker@codex-01.service"}
    else:
        raise AssertionError("the failed worker restart should have raised an operation error")
    finish_operation(client, "node1", token, operation, failure, status="failed")

    assert read_env_file(project_file)["SERVICE_TOKEN"] == previous
    assert restarted == ["aiflow4-worker@codex-01.service", "aiflow4-worker@codex-01.service"]
    status = client.get(f"/v1/runtime-settings/changes/{saved.json()['change_id']}")
    assert status.status_code == 200 and status.json()["status"] == "failed"
    failed = next(item for item in status.json()["operations"] if item["node"] == "node1")
    assert failed["result"]["failed_service"] == "aiflow4-worker@codex-01.service"
    assert "previous value restored" in failed["result"]["error"]
    assert replacement not in status.text


def test_active_work_is_not_restarted_when_drain_times_out(client, ctx, tmp_path, monkeypatch):
    project_dir = tmp_path / "project-env"
    project_dir.mkdir()
    project_file = project_dir / "TESTWP.env"
    project_file.write_text("FEATURE_MODE=before\n")
    control_dir = tmp_path / "control-projects"
    control_dir.mkdir()
    ctx.settings.project_env_dir = str(control_dir)
    join_and_heartbeat(client, "control", role="control", settings_dir=control_dir)
    token = join_and_heartbeat(client, "node1")
    sign_in(client, ctx)
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", project_dir)
    monkeypatch.setattr(operations, "_wait_for_worker_idle", lambda _units: False)
    restarted = []
    monkeypatch.setattr(operations, "_restart_worker", lambda unit: restarted.append(unit) or True)

    queued = client.post("/v1/runtime-settings", json={"scope": "project", "project": "TESTWP",
                                                        "key": "FEATURE_MODE", "value": "after"})
    assert queued.status_code == 202
    operation = next_operation(client, "node1", token)
    try:
        operations.execute(operation)
    except operations.OperationError as error:
        failure = {"healthy": False, "error": str(error)}
    else:
        raise AssertionError("workers must not be restarted before active work drains")
    finish_operation(client, "node1", token, operation, failure, status="failed")

    assert read_env_file(project_file)["FEATURE_MODE"] == "before"
    assert restarted == []
    result = client.get(f"/v1/runtime-settings/changes/{queued.json()['change_id']}").json()
    assert result["status"] == "failed"
    failed = next(item for item in result["operations"] if item["node"] == "node1")
    assert "active worker did not become idle" in failed["result"]["error"]


def test_project_failure_rolls_back_successful_peer_before_resuming_work(client, ctx, tmp_path, monkeypatch):
    first_dir, second_dir = tmp_path / "node1-projects", tmp_path / "node2-projects"
    first_dir.mkdir()
    second_dir.mkdir()
    first_file, second_file = first_dir / "TESTWP.env", second_dir / "TESTWP.env"
    first_file.write_text("SERVICE_TOKEN=old-node-one\n")
    second_file.write_text("SERVICE_TOKEN=old-node-two\n")
    control_dir = tmp_path / "control-projects"
    control_dir.mkdir()
    control_file = control_dir / "TESTWP.env"
    control_file.write_text("SERVICE_TOKEN=old-control-copy\n")
    ctx.settings.project_env_dir = str(control_dir)
    control_token = join_and_heartbeat(client, "control", role="control", settings_dir=control_dir)
    first_token = join_and_heartbeat(client, "node1", settings_dir=first_dir)
    second_token = join_and_heartbeat(client, "node2", settings_dir=second_dir)
    ctx.repo().nodes["node2"] = Node(name="node2", projects=["TESTWP"])
    ctx.repo().accounts.append(Account(id="codex-02", provider="codex", node="node2", tier="middle",
                                       roles=["dev", "verify", "master"]))
    sign_in(client, ctx)
    mount_ui(client.app, ctx)
    ctx.settings.project_env_dir = str(control_dir)
    monkeypatch.setattr(operations, "_wait_for_worker_idle", lambda _units: True)
    active_node = {"name": "node1"}
    restarts = {"node1": 0, "node2": 0}

    def restart(_unit):
        restarts[active_node["name"]] += 1
        # node2's first restart fails, while its own restore restart and node1's coordinated rollback pass.
        return active_node["name"] != "node2" or restarts["node2"] > 1

    monkeypatch.setattr(operations, "_restart_worker", restart)
    saved_value = secrets.token_urlsafe(20)
    queued = client.post("/v1/runtime-settings", json={"scope": "project", "project": "TESTWP",
                                                        "key": "SERVICE_TOKEN", "value": saved_value})
    assert queued.status_code == 202

    active_node["name"] = "control"
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", control_dir)
    control_operation = next_operation(client, "control", control_token)
    control_result = operations.execute(control_operation)
    finish_operation(client, "control", control_token, control_operation, control_result)
    assert read_env_file(control_file)["SERVICE_TOKEN"] == saved_value

    active_node["name"] = "node1"
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", first_dir)
    first_operation = next_operation(client, "node1", first_token)
    # The first agent applies the value and dies before reporting success. Its encrypted original snapshot must
    # already be durable in the operation so a reclaimed retry cannot snapshot the newly written file.
    first_result = operations.execute(
        first_operation,
        checkpoint=lambda envelope: checkpoint_operation(client, "node1", first_token, first_operation, envelope))
    assert saved_value not in json.dumps(first_result)
    persisted = ctx.db.one("SELECT payload FROM node_operations WHERE id=%s", (first_operation["id"],))["payload"]
    assert persisted["rollback_envelope"] == first_result["rollback_envelope"]
    assert read_env_file(first_file)["SERVICE_TOKEN"] == saved_value
    assert ctx.db.one("SELECT accepting_work FROM nodes WHERE name='node1'")["accepting_work"] is False

    with ctx.db.pool.connection() as connection:
        connection.execute("UPDATE node_operations SET started_at=now()-interval '31 minutes' WHERE id=%s",
                           (first_operation["id"],))
    retried_operation = next_operation(client, "node1", first_token)
    assert retried_operation["id"] == first_operation["id"]
    assert retried_operation["payload"]["rollback_envelope"] == first_result["rollback_envelope"]
    retry_result = operations.execute(retried_operation)
    assert retry_result["rollback_envelope"] == first_result["rollback_envelope"]
    finish_operation(client, "node1", first_token, retried_operation, retry_result)

    active_node["name"] = "node2"
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", second_dir)
    second_operation = next_operation(client, "node2", second_token)
    try:
        operations.execute(second_operation)
    except operations.OperationError as error:
        failure = {"healthy": False, "error": str(error),
                   "failed_service": "aiflow4-worker@codex-02.service"}
    else:
        raise AssertionError("node2's worker restart should have failed")
    finish_operation(client, "node2", second_token, second_operation, failure, status="failed")
    assert read_env_file(second_file)["SERVICE_TOKEN"] == "old-node-two"
    assert ctx.db.one("SELECT accepting_work FROM nodes WHERE name='node1'")["accepting_work"] is False
    assert ctx.db.one("SELECT accepting_work FROM nodes WHERE name='node2'")["accepting_work"] is False

    active_node["name"] = "node1"
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", first_dir)
    rollback = next_operation(client, "node1", first_token)
    assert rollback["payload"]["action"] == "runtime_setting_rollback"
    rollback_result = operations.execute(rollback)
    assert saved_value not in json.dumps(rollback_result)
    finish_operation(client, "node1", first_token, rollback, rollback_result)

    active_node["name"] = "control"
    monkeypatch.setattr(operations, "PROJECT_ENV_DIR", control_dir)
    control_rollback = next_operation(client, "control", control_token)
    assert control_rollback["payload"]["action"] == "runtime_setting_rollback"
    control_rollback_result = operations.execute(control_rollback)
    finish_operation(client, "control", control_token, control_rollback, control_rollback_result)

    assert read_env_file(first_file)["SERVICE_TOKEN"] == "old-node-one"
    assert read_env_file(second_file)["SERVICE_TOKEN"] == "old-node-two"
    assert read_env_file(control_file)["SERVICE_TOKEN"] == "old-control-copy"
    assert ctx.db.one("SELECT accepting_work FROM nodes WHERE name='node1'")["accepting_work"] is True
    assert ctx.db.one("SELECT accepting_work FROM nodes WHERE name='node2'")["accepting_work"] is True
    result = client.get(f"/v1/runtime-settings/changes/{queued.json()['change_id']}")
    assert result.status_code == 200 and result.json()["status"] == "failed"
    assert result.json()["recovery_status"] == "restored"
    assert len(result.json()["rollbacks"]) == 2
    assert all(row["status"] == "completed" for row in result.json()["rollbacks"])
    assert saved_value not in result.text and "rollback_envelope" not in result.text
    page = client.get("/settings")
    assert "previous value was restored on all affected machines" in page.text
    assert "previous value could not be confirmed across the fleet" in page.text


def test_control_backup_credentials_are_refused_instead_of_claiming_to_update_control_loops(client, ctx):
    sign_in(client, ctx)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        response = client.post("/v1/runtime-settings", json={"scope": "secret", "key": key,
                                                               "value": "opaque-test-value"})
        assert response.status_code == 422
        assert "control-loops" not in response.text
        assert "opaque-test-value" not in response.text
    assert ctx.db.one("SELECT count(*) AS n FROM node_operations WHERE action LIKE 'runtime-setting%%'")["n"] == 0


def test_runtime_setting_timestamps_change_per_key_not_per_file(tmp_path):
    project_dir = tmp_path / "projects"
    project_dir.mkdir()
    project_file = project_dir / "TESTWP.env"
    project_file.write_text("FIRST=old\nSECOND=unchanged\n")
    before = {item["name"]: item["changed_at"]
              for item in node_agent.runtime_settings_inventory(project_dir)["TESTWP"]}

    update_env(project_file, force={"FIRST": "new"}, track_timestamps=True)
    after = {item["name"]: item["changed_at"]
             for item in node_agent.runtime_settings_inventory(project_dir)["TESTWP"]}

    assert after["FIRST"] != before["FIRST"]
    assert after["SECOND"] == before["SECOND"]

    control_after = {item["name"]: item["changed_at"]
                     for item in runtime_settings._file_rows(project_file, "project", "TESTWP")}
    assert control_after == after


def test_replacement_worker_receives_backed_up_project_values_before_starting_workers(
        client, ctx, tmp_path, monkeypatch):
    control_dir = tmp_path / "control-projects"
    control_dir.mkdir()
    source = control_dir / "TESTWP.env"
    update_env(source, force={"SERVICE_TOKEN": "central-copy"}, track_timestamps=True)
    ctx.settings.project_env_dir = str(control_dir)

    key_file = tmp_path / "replacement-node.key"
    monkeypatch.setenv("AIFLOW4_NODE_OPERATION_KEY_FILE", str(key_file))
    public_key = node_public_key(key_file)
    joined = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": "replacement",
                                                         "role": "worker", "project": "TESTWP"})
    assert joined.status_code == 200
    enrolled = client.post("/v1/nodes/join", json={"join_token": joined.json()["token"]})
    assert enrolled.status_code == 200
    token = enrolled.json()["node_token"]
    response = client.post("/v1/nodes/replacement/heartbeat", headers={"Authorization": f"Bearer {token}"}, json={
        "slots_total": 2, "slots_used": 0, "release_version": "v4.0.0", "release_protocol": 1,
        "runtime_settings": {}, "runtime_settings_public_key": public_key,
    })
    assert response.status_code == 200, response.text
    entries = response.json()["desired"]["runtime_settings_bootstrap"]
    assert len(entries) == 1
    assert "central-copy" not in response.text

    replacement_dir = tmp_path / "replacement-projects"
    replacement_dir.mkdir()
    node_agent.apply_runtime_settings_bootstrap("replacement", entries, replacement_dir)
    assert read_env_file(replacement_dir / "TESTWP.env")["SERVICE_TOKEN"] == "central-copy"
    assert node_agent.runtime_settings_inventory(replacement_dir)["TESTWP"][0]["changed_at"] == entries[0]["changed_at"]
