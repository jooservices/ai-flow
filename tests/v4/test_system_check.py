"""System readiness results, trigger scheduling, work gate, UI and support export."""
from __future__ import annotations

import json
from pathlib import Path

from aiflow.v4.control import loops, system_check
from aiflow.v4.control import notify
from aiflow.v4.core.system_check import condition
from aiflow.v4.node import agent
from conftest import add_ticket, join_node


def _direct_join(client, name="cc"):
    grant = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": name,
                                                        "role": "worker", "project": "TESTWP"})
    assert grant.status_code == 200
    joined = client.post("/v1/nodes/join", json={"join_token": grant.json()["token"]})
    assert joined.status_code == 200
    return {"Authorization": f"Bearer {joined.json()['node_token']}"}


def test_machine_join_project_add_upgrade_and_hourly_are_check_triggers(client, ctx, control_repo, monkeypatch):
    headers = _direct_join(client)
    row = ctx.db.one("SELECT readiness FROM nodes WHERE name='cc'")
    assert row["readiness"]["trigger"] == "machine_joined"

    joined = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {joined['node_token']}"}
    client.post("/v1/nodes/cc/heartbeat", json={"release_version": "v4.1.0"}, headers=headers)
    assert ctx.db.one("SELECT readiness FROM nodes WHERE name='cc'")["readiness"]["trigger"] == "heartbeat"
    client.post("/v1/nodes/cc/heartbeat", json={"release_version": "v4.2.0"}, headers=headers)
    assert ctx.db.one("SELECT readiness FROM nodes WHERE name='cc'")["readiness"]["trigger"] == "upgrade"

    from tests.v4.test_projects import owner_sign_in, prepare_project_control, project_body

    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    added = client.post("/v1/projects", json=project_body())
    assert added.status_code == 200, added.text
    rows = {row["name"]: row for row in client.get("/v1/system-check").json()["nodes"]}
    assert rows["cc"]["readiness"]["trigger"] == "project_added"
    assert "NEWAPP" in rows["cc"]["readiness"]["projects"]

    monkeypatch.setattr(ctx.leases, "leader", lambda *args, **kwargs: True)
    monkeypatch.setattr(loops, "jira_sync", lambda *args, **kwargs: 0)
    monkeypatch.setattr(loops, "watchdog", lambda *args, **kwargs: None)
    monkeypatch.setattr(loops, "fleet_rollout", lambda *args, **kwargs: None)
    monkeypatch.setattr(loops, "version_release_scan", lambda *args, **kwargs: 0)
    monkeypatch.setattr(loops.memory, "index", lambda *args, **kwargs: {})
    checked = []
    monkeypatch.setattr(loops.system_check, "refresh_all", lambda _ctx, trigger: checked.append(trigger))

    class StopAfterFirstCycle:
        done = False

        def is_set(self):
            return self.done

        def wait(self, _seconds):
            self.done = True

    loops.run_loops(ctx, StopAfterFirstCycle())
    assert checked == ["hourly"]


def test_all_four_results_and_action_text_are_reported_per_project(client, ctx):
    project = ctx.repo().projects["TESTWP"]
    project.stack = "php-8.3"
    project.services = ["postgres"]
    ctx.repo().versions["tools"].update({"php": "8.3", "composer": "2.8", "wp": "2.12"})
    headers = join_node(client, "cc").json()
    node_headers = {"Authorization": f"Bearer {headers['node_token']}"}
    tools = ctx.db.one("SELECT tools FROM nodes WHERE name='cc'")["tools"]
    tools["composer"] = "missing"
    credentials = {account.id: {"present": True, "valid": True}
                   for account in ctx.repo().accounts_on("cc")}
    response = client.post("/v1/nodes/cc/heartbeat", headers=node_headers, json={
        "tools": tools, "metrics": {"disk_free": 10 * 1024 ** 3},
        "services": {"postgres": {"state": "started", "healthy": True, "detail": "healthy"}},
        "system_conditions": [condition("disk-space", "Free disk space", "ready", "10 GiB is available."),
                              condition("workspace-permissions", "Work directory permissions", "ready",
                                        "The node can write its work and state directories."),
                              condition("control-network", "Control API network", "ready", "Heartbeat reached control."),
                              condition("repository-network", "Repository network", "ready", "GitHub is reachable.")],
        "credential_checks": credentials, "auto_fixed_tools": ["php"],
    })
    assert response.status_code == 200
    results = response.json()["desired"]  # the check itself is available from the persisted report below
    del results
    project_results = client.get("/v1/system-check").json()["nodes"][0]["readiness"]["projects"]["TESTWP"]
    by_id = {item["id"]: item for item in project_results}
    assert by_id["tool:php"]["result"] == "fixed_automatically"
    assert by_id["tool:git"]["result"] == "ready"
    assert by_id["tool:composer"]["result"] == "needs_action"
    assert by_id["tool:composer"]["action"] == "Install or repair composer on node cc."
    assert by_id["service:postgres"]["result"] == "fixed_automatically"
    machine = client.get("/v1/system-check").json()["nodes"][0]["readiness"]["machine"]
    assert next(item for item in machine if item["id"] == "node-heartbeat")["result"] == "ready"

    project.stack = "unknown-stack"
    client.post("/v1/nodes/cc/heartbeat", headers=node_headers, json={"tools": tools})
    project_results = client.get("/v1/system-check").json()["nodes"][0]["readiness"]["projects"]["TESTWP"]
    assert next(item for item in project_results if item["id"] == "stack-support")["result"] == \
        "not_possible_on_this_machine"


def test_only_work_affecting_failures_alert_and_work_gate_names_failed_condition(client, ctx, monkeypatch):
    notifications = []
    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: notifications.append((args, kwargs)) or [])
    repo = ctx.repo()
    repo.projects["TESTWP"].stack = "php-8.3"
    repo.versions["tools"].update({"php": "8.3", "composer": "2.8", "wp": "2.12"})
    joined = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {joined['node_token']}"}
    machine_ready = [condition("disk-space", "Free disk space", "ready", "20 GiB is available."),
                     condition("workspace-permissions", "Work directory permissions", "ready",
                               "The node can write its work and state directories."),
                     condition("control-network", "Control API network", "ready", "Heartbeat reached control."),
                     condition("repository-network", "Repository network", "ready", "GitHub is reachable.")]
    ready = condition("diagnostic-note", "Diagnostic note", "ready", "Optional hint.", affects_work=False)
    client.post("/v1/nodes/cc/heartbeat", json={"system_conditions": machine_ready + [ready]}, headers=headers)
    assert notifications == []
    client.post("/v1/nodes/cc/heartbeat", json={
        "system_conditions": machine_ready + [condition("diagnostic-note", "Diagnostic note", "needs_action",
                                                          "Optional hint.", "Review the optional hint.",
                                                          affects_work=False)]}, headers=headers)
    assert notifications == []

    tools = ctx.db.one("SELECT tools FROM nodes WHERE name='cc'")["tools"]
    tools["composer"] = "missing"
    client.post("/v1/nodes/cc/heartbeat", json={
        "tools": tools,
        "system_conditions": [condition("disk-space", "Free disk space", "needs_action",
                                         "Only 1 GiB is available.", "Free at least 5 GiB on /srv/aiflow."),
                              *machine_ready[1:]],
    }, headers=headers)
    assert len(notifications) == 2  # composer and disk now prevent work
    add_ticket(ctx.db, "TESTWP-249")
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-249", "account": "claude-01"}, headers=headers)
    assert claim.status_code == 409

    project = next(p for p in client.get("/v1/projects").json()["projects"] if p["key"] == "TESTWP")
    node_result = next(n for n in project["node_readiness"] if n["name"] == "cc")
    assert node_result["blocked"]
    assert {c["name"] for c in node_result["conditions"]} >= {"Tool · composer", "Free disk space"}

    client.post("/v1/nodes/cc/heartbeat", json={
        "system_conditions": [condition("disk-space", "Free disk space", "fixed_automatically",
                                         "The node freed space automatically; 6 GiB is available."),
                              *machine_ready[1:]],
        "metrics": {"disk_free": 6 * 1024 ** 3},
    }, headers=headers)
    assert len(notifications) == 2

    from aiflow.v4.control.db import J
    ctx.db.run("UPDATE accounts SET worker_state=%s WHERE id='claude-01'",
               (J({"probe": {"ok": False, "summary": "SECRET-CREDENTIAL-VALUE"}}),))
    bundle = client.get("/v1/support-bundle")
    assert bundle.status_code == 200
    payload = bundle.json()
    assert payload["system_check"]["nodes"][0]["readiness"]["projects"]["TESTWP"]
    assert "SECRET-CREDENTIAL-VALUE" not in json.dumps(payload)


def test_system_check_screen_and_clean_install_acceptance_include_results():
    root = Path(__file__).resolve().parents[2]
    page = (root / "aiflow/v4/control/ui/console.html").read_text(encoding="utf-8")
    assert 'data-view="system-check">System check</button>' in page
    assert 'id="view-system-check"' in page and "fetch('/v1/system-check'" in page
    assert "Ready" in page and "Fixed automatically" in page and "Needs an action" in page
    assert "Not possible on this machine" in page and "/v1/support-bundle" in page
    assert "c.name + ' — ' + (c.action || c.detail)" in page
    run_handler = page.split("else if (act === 'system-check-run') {", 1)[1].split("\n  else if", 1)[0]
    assert "fetch('/v1/system-check/run'" in run_handler
    assert "fetch('/v1/system-check', { cache: 'no-store' })" in run_handler
    assert "S.systemCheck = d" in run_handler
    smoke = (root / "deploy/v4/ci/install-smoke.sh").read_text(encoding="utf-8")
    assert "/v1/system-check" in smoke and "/v1/support-bundle" in smoke


def test_node_agent_sends_scrubbed_credential_and_machine_results(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "_github_network_available", lambda: True)
    machine = agent._system_machine_conditions({"disk_free": 20 * 1024 ** 3}, work_path=tmp_path / "work",
                                               state_path=tmp_path / "state")
    assert {item["id"] for item in machine} >= {"disk-space", "workspace-permissions", "control-network",
                                                   "repository-network"}
    assert next(item for item in machine if item["id"] == "workspace-permissions")["result"] == \
        "fixed_automatically"
    checks = agent._credential_conditions([{"account": "codex-01", "state": "idle", "alive": True,
                                            "probe": {"ok": False, "error_kind": "auth",
                                                      "summary": "SECRET-CREDENTIAL-VALUE"}}])
    encoded = json.dumps(checks)
    assert checks["codex-01"] == {"present": True, "valid": False}
    assert "SECRET-CREDENTIAL-VALUE" not in encoded


def test_credential_heartbeat_is_rebuilt_from_boolean_status_fields_and_bundle_is_scrubbed(client, ctx):
    joined = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {joined['node_token']}"}
    account_id = ctx.repo().accounts_on("cc")[0].id
    payload_secret = "SECRET-CREDENTIAL-FIELD"
    client.post("/v1/nodes/cc/heartbeat", headers=headers, json={
        "credential_checks": {
            account_id: {"present": True, "valid": False, "detail": payload_secret,
                         "action": payload_secret, "provider_output": payload_secret},
            "unassigned-secret-account": {"present": True, "valid": True, "detail": payload_secret},
        },
    })

    readiness = client.get("/v1/system-check").json()["nodes"][0]["readiness"]
    credential = next(item for item in readiness["projects"]["TESTWP"]
                      if item["id"] == f"credential:{account_id}")
    assert credential["result"] == "needs_action"
    assert credential["detail"] == "Credential is invalid."
    assert credential["action"] == f"Sign in again to AI account {account_id} on this machine."
    assert readiness["credential_statuses"] == {account_id: {"present": True, "valid": False}}
    assert "credential_inputs" not in readiness
    assert payload_secret not in json.dumps(readiness)

    bundle = client.get("/v1/support-bundle").json()
    assert payload_secret not in json.dumps(bundle)
    assert bundle["system_check"]["nodes"][0]["readiness"]["credential_statuses"] == {
        account_id: {"present": True, "valid": False}}


def test_legacy_credential_rows_are_scrubbed_from_report_and_bundle(client, ctx):
    join_node(client, "cc")
    account_id = ctx.repo().accounts_on("cc")[0].id
    secret = "LEGACY-STORED-CREDENTIAL-OUTPUT"
    from aiflow.v4.control.db import J

    ctx.db.run("UPDATE nodes SET readiness=%s WHERE name='cc'", (J({
        "machine": [],
        "projects": {"TESTWP": [{"id": secret, "name": secret,
                                 "result": "needs_action", "detail": secret, "action": secret,
                                 "provider_output": secret}]},
        "credential_inputs": {account_id: {"detail": secret}},
        "checked_at": "2026-10-22T00:00:00+00:00",
    }),))

    report = client.get("/v1/system-check").json()
    bundle = client.get("/v1/support-bundle").json()
    for payload in (report, bundle):
        encoded = json.dumps(payload)
        assert secret not in encoded
        readiness = (payload["nodes"][0]["readiness"] if payload is report
                     else payload["system_check"]["nodes"][0]["readiness"])
        credential = readiness["projects"]["TESTWP"][0]
        assert credential["detail"] == "Credential status has not been confirmed."
        assert credential["action"] == f"Sign in to AI account {account_id} on this machine."
        assert "credential_inputs" not in readiness
