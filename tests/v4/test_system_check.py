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
    diagnostic = {"result": "ready"}
    build_project_conditions = system_check._project_conditions

    def with_optional_diagnostic(*args, **kwargs):
        rows = build_project_conditions(*args, **kwargs)
        rows.append(condition("diagnostic-note", "Diagnostic note", diagnostic["result"], "Optional hint.",
                              "Review the optional hint.", affects_work=False))
        return rows

    monkeypatch.setattr(system_check, "_project_conditions", with_optional_diagnostic)
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
    client.post("/v1/nodes/cc/heartbeat", json={"system_conditions": machine_ready}, headers=headers)
    assert notifications == []
    diagnostic["result"] = "needs_action"
    client.post("/v1/nodes/cc/heartbeat", json={"system_conditions": machine_ready}, headers=headers)
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


def test_hourly_system_check_revalidates_repository_branch_and_jira_access(client, ctx, monkeypatch):
    notifications = []
    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: notifications.append((args, kwargs)) or [])
    joined = join_node(client, "cc").json()
    ctx.github.available_branches[("test-aiflow-wp", "develop")] = False
    branch_result = system_check.refresh_node(ctx, "cc", "hourly")
    branch = next(item for item in branch_result["projects"]["TESTWP"]
                  if item["id"] == "base-branch:jooservices/test-aiflow-wp")
    assert branch["result"] == "needs_action"

    ctx.github.repositories = []

    from aiflow.v4.control.jira import JiraError

    secret = "SECRET-JIRA-RESPONSE-TOKEN"

    def revoked_jira_credential():
        raise JiraError(f"GET /rest/api/3/project/search -> 401: {secret}")

    ctx.jira.projects = revoked_jira_credential
    result = system_check.refresh_node(ctx, "cc", "manual")
    rows = result["projects"]["TESTWP"]
    by_id = {item["id"]: item for item in rows}
    assert by_id["github-installation"]["result"] == "ready"
    assert by_id["repository-access:jooservices/test-aiflow-wp"]["result"] == "needs_action"
    assert by_id["base-branch:jooservices/test-aiflow-wp"]["result"] == "needs_action"
    assert by_id["jira-credentials"]["result"] == "needs_action"
    assert by_id["jira-credentials"]["detail"] == "Jira credential is invalid."
    assert secret not in json.dumps(result)
    assert notifications

    project = next(p for p in client.get("/v1/projects").json()["projects"] if p["key"] == "TESTWP")
    node_result = next(n for n in project["node_readiness"] if n["name"] == "cc")
    assert node_result["blocked"]
    assert {row["id"] for row in node_result["conditions"]} >= {
        "repository-access:jooservices/test-aiflow-wp", "base-branch:jooservices/test-aiflow-wp",
        "jira-credentials"}


def test_jira_browse_access_without_issue_write_permissions_needs_action(client, ctx):
    ctx.jira.project_permissions["TESTWP"] = {
        "EDIT_ISSUES": False, "TRANSITION_ISSUES": False, "ADD_COMMENTS": True,
    }
    joined = join_node(client, "cc").json()
    readiness = client.get("/v1/system-check").json()["nodes"][0]["readiness"]
    conditions = {item["id"]: item for item in readiness["projects"]["TESTWP"]}

    assert ctx.jira.project_permission_reads == ["TESTWP"]
    assert conditions["jira-credentials"]["result"] == "ready"  # the browse/list request still succeeds
    permission = conditions["jira-write-permissions"]
    assert permission["result"] == "needs_action"
    assert permission["detail"] == (
        "The connected Jira account is missing Edit Issues and Transition Issues permissions in project TESTWP.")
    assert permission["action"] == (
        "Grant the connected Jira account Edit Issues and Transition Issues permissions in project TESTWP.")
    assert joined["node"] == "cc"


def test_jira_edit_and_transition_without_comment_permission_needs_action(client, ctx):
    ctx.jira.project_permissions["TESTWP"] = {
        "EDIT_ISSUES": True, "TRANSITION_ISSUES": True, "ADD_COMMENTS": False,
    }
    join_node(client, "cc")
    readiness = client.get("/v1/system-check").json()["nodes"][0]["readiness"]
    conditions = {item["id"]: item for item in readiness["projects"]["TESTWP"]}

    permission = conditions["jira-write-permissions"]
    assert permission["result"] == "needs_action"
    assert permission["detail"] == (
        "The connected Jira account is missing Add Comments permission in project TESTWP.")
    assert permission["action"] == (
        "Grant the connected Jira account Add Comments permission in project TESTWP.")


def test_projects_readiness_shows_account_failures_but_blocks_only_without_a_usable_account(client, ctx):
    joined = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {joined['node_token']}"}
    response = client.post("/v1/nodes/cc/heartbeat", headers=headers, json={
        "credential_checks": {
            "claude-01": {"present": True, "valid": True},
            "opencode-01": {"present": True, "valid": False},
        },
    })
    assert response.status_code == 200

    project = next(item for item in client.get("/v1/projects").json()["projects"] if item["key"] == "TESTWP")
    node_result = next(item for item in project["node_readiness"] if item["name"] == "cc")
    account_failure = next(item for item in node_result["conditions"] if item["id"] == "credential:opencode-01")
    assert account_failure["result"] == "needs_action"
    assert account_failure["detail"] == "Credential is invalid."
    assert node_result["blocked"] is False

    add_ticket(ctx.db, "TESTWP-249")
    candidates = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers)
    assert candidates.status_code == 200
    assert "TESTWP-249" in [ticket["key"] for ticket in candidates.json()["tickets"]]


def test_support_bundle_omits_untrusted_heartbeat_maps(client):
    joined = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {joined['node_token']}"}
    secret = "SECRET-EXTRA-HEARTBEAT-FIELD"
    client.post("/v1/nodes/cc/heartbeat", headers=headers, json={
        "tools": {"unexpected-tool-field": secret},
        "metrics": {"future-metric-field": secret},
    })

    bundle = client.get("/v1/support-bundle").json()
    encoded = json.dumps(bundle)
    assert secret not in encoded
    assert "unexpected-tool-field" not in encoded and "future-metric-field" not in encoded
    assert "tools" not in bundle["system_check"]["nodes"][0]
    assert "metrics" not in bundle["system_check"]["nodes"][0]


def test_system_check_screen_and_clean_install_acceptance_include_results():
    root = Path(__file__).resolve().parents[2]
    page = (root / "aiflow/v4/control/ui/console.html").read_text(encoding="utf-8")
    assert 'data-view="system-check">System check</button>' in page
    assert 'id="view-system-check"' in page and "fetch('/v1/system-check'" in page
    assert "Ready" in page and "Fixed automatically" in page and "Needs an action" in page
    assert "Not possible on this machine" in page and "/v1/support-bundle" in page
    assert "esc(c.name || c.id) + ' — ' + esc(c.action || c.detail)" in page
    run_handler = page.split("else if (act === 'system-check-run') {", 1)[1].split("\n  else if", 1)[0]
    assert "fetch('/v1/system-check/run'" in run_handler
    assert "fetch('/v1/system-check', { cache: 'no-store' })" in run_handler
    assert "S.systemCheck = d" in run_handler
    smoke = (root / "deploy/v4/ci/install-smoke.sh").read_text(encoding="utf-8")
    assert "/v1/system-check" in smoke and "/v1/support-bundle" in smoke


def test_node_agent_sends_scrubbed_credential_and_machine_results(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "_github_network_available", lambda: True)
    monkeypatch.setattr(agent, "_worker_can_access_directory", lambda _path, _uid: True)
    machine = agent._system_machine_conditions({"disk_free": 20 * 1024 ** 3}, work_path=tmp_path / "work",
                                               state_path=tmp_path / "state")
    assert {item["id"] for item in machine} >= {"disk-space", "workspace-permissions", "control-network",
                                                   "repository-network"}
    assert next(item for item in machine if item["id"] == "workspace-permissions")["result"] == \
        "fixed_automatically"
    import pwd
    worker_uid = pwd.getpwnam("aiflow-worker").pw_uid
    assert (tmp_path / "work").stat().st_uid == worker_uid
    assert (tmp_path / "state").stat().st_uid == worker_uid
    checks = agent._credential_conditions([{"account": "codex-01", "state": "idle", "alive": True,
                                            "probe": {"ok": False, "error_kind": "auth",
                                                      "summary": "SECRET-CREDENTIAL-VALUE"}}])
    encoded = json.dumps(checks)
    assert checks["codex-01"] == {"present": True, "valid": False}
    assert "SECRET-CREDENTIAL-VALUE" not in encoded


def test_primary_credential_probe_is_not_overwritten_by_an_extra_session():
    primary = {"account": "codex-01", "instance": "codex-01", "state": "idle", "alive": True,
               "probe": {"ok": True}}
    extra = {"account": "codex-01", "instance": "codex-01.2", "state": "idle", "alive": True}

    assert agent._credential_conditions([primary, extra]) == {"codex-01": {"present": True, "valid": True}}
    assert agent._credential_conditions([extra, primary]) == {"codex-01": {"present": True, "valid": True}}
    assert agent._credential_conditions([extra]) == {"codex-01": {"present": True, "valid": None}}


def test_workspace_permission_check_reports_a_directory_the_worker_cannot_write(tmp_path):
    work, state = tmp_path / "locked-work", tmp_path / "state"
    work.mkdir()
    state.mkdir()
    work.chmod(0o555)
    import pwd
    assert not agent._worker_can_access_directory(work, pwd.getpwnam("aiflow-worker").pw_uid)

    machine = agent._system_machine_conditions({}, work_path=work, state_path=state)
    workspace = next(item for item in machine if item["id"] == "workspace-permissions")
    assert workspace["result"] == "needs_action"
    assert "cannot write" in workspace["detail"]
    assert "aiflow service account" in workspace["action"]


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


def test_secret_bearing_machine_conditions_are_normalized_before_storage_report_and_bundle(client, ctx):
    joined = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {joined['node_token']}"}
    secret = "SECRET-MACHINE-CONDITION-TOKEN"
    client.post("/v1/nodes/cc/heartbeat", headers=headers, json={
        "system_conditions": [{
            "id": "disk-space", "name": f"<img>{secret}", "result": "needs_action",
            "detail": f"provider output: {secret}", "action": f"send token {secret}",
            "affects_work": True, "provider_output": secret,
        }],
    })

    readiness = client.get("/v1/system-check").json()["nodes"][0]["readiness"]
    disk = next(item for item in readiness["machine"] if item["id"] == "disk-space")
    assert disk["name"] == "Free disk space"
    assert disk["action"] == "Free at least 5 GiB on /srv/aiflow."
    assert "machine_inputs" not in readiness
    assert secret not in json.dumps(readiness)

    stored = ctx.db.one("SELECT readiness FROM nodes WHERE name='cc'")["readiness"]
    assert "machine_inputs" not in stored
    assert secret not in json.dumps(stored)
    bundle = client.get("/v1/support-bundle").json()
    assert secret not in json.dumps(bundle)


def test_unknown_credential_validity_blocks_work_until_confirmed(client, ctx):
    joined = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {joined['node_token']}"}
    client.post("/v1/nodes/cc/heartbeat", headers=headers, json={
        "credential_checks": {"claude-01": {"present": True, "valid": None}},
    })
    readiness = client.get("/v1/system-check").json()["nodes"][0]["readiness"]
    credential = next(item for item in readiness["projects"]["TESTWP"]
                      if item["id"] == "credential:claude-01")
    assert credential["result"] == "needs_action" and credential["affects_work"] is True

    add_ticket(ctx.db, "TESTWP-250")
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-250", "account": "claude-01"}, headers=headers)
    assert claim.status_code == 409


def test_missing_readiness_snapshot_fails_closed_for_work_and_projects_explain_why(client, ctx):
    joined = join_node(client, "cc").json()
    headers = {"Authorization": f"Bearer {joined['node_token']}"}
    from aiflow.v4.control.db import J

    ctx.db.run("UPDATE nodes SET readiness=%s, readiness_checked_at=NULL WHERE name='cc'", (J({}),))
    add_ticket(ctx.db, "TESTWP-251")
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-251", "account": "claude-01"}, headers=headers)
    assert claim.status_code == 409

    project = next(p for p in client.get("/v1/projects").json()["projects"] if p["key"] == "TESTWP")
    node_result = next(n for n in project["node_readiness"] if n["name"] == "cc")
    assert node_result["blocked"]
    assert node_result["conditions"][0]["id"] == "system-check-snapshot"
    assert "first system readiness check" in node_result["conditions"][0]["detail"]


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
