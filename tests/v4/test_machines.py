from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from conftest import add_ticket, join_node
from console_test_utils import console_script
from aiflow.v4.control import api as control_api
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session


def owner_session(client, ctx, role="owner"):
    secret = "test-console-session-secret-at-least-32-bytes"
    ctx.settings.console_session_secret = secret
    client.cookies.set(COOKIE_NAME, issue_session("owner@example.test", role, int(time.time()) + 3600, secret))


def settle_heartbeat_readiness(ctx, node):
    """Wait for the provider-backed readiness check before asserting worker eligibility."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        pending = [thread for thread in threading.enumerate()
                   if thread.name == f"aiflow4-heartbeat-readiness-{node}"]
        if not pending:
            break
        for thread in pending:
            thread.join(timeout=min(0.1, max(0, deadline - time.monotonic())))
    else:
        raise AssertionError(f"heartbeat readiness check for {node} did not settle")
    from aiflow.v4.control.system_check import refresh_node
    readiness = ctx.db.one("SELECT readiness FROM nodes WHERE name=%s", (node,)) or {}
    refresh_node(ctx, node, "manual", credential_checks=(readiness.get("readiness") or {}).get(
        "credential_statuses", {}))


def release_fixture(monkeypatch, tmp_path):
    monkeypatch.setattr(control_api, "RELEASES", tmp_path)
    (tmp_path / "CURRENT").write_text("v1.2.3")
    directory = tmp_path / "v1.2.3"
    directory.mkdir()
    (directory / "aiflow4-v1.2.3.tar.gz").write_bytes(b"release")
    (directory / "aiflow4-v1.2.3.tar.gz.sigstore.json").write_text("{}")
    (directory / "SHA256SUMS").write_text("0" * 64 + "  aiflow4-v1.2.3.tar.gz\n")


def test_machine_screen_renders_machine_specs_account_controls_machine_usage_and_join_countdown():
    node_js = shutil.which("node")
    if not node_js:
        import pytest
        pytest.skip("Node.js is needed to render the console screen")
    script = console_script(Path("aiflow/v4/control/ui/console.html"))
    driver = r'''
var machinesView = {innerHTML: ''};
document.querySelector = function (selector) { return selector === '#view-machines' ? machinesView : null; };
S.consoleUser = {permissions: ['nodes.manage']};
S.data = {nodes: [{name:'node-ui',status:'online',has_agent:true,accepting_work:true,slots_total:8,sessions_busy:2,
  metrics:{mem_used:1073741824,mem_total:8589934592,cpus:8},last_heartbeat:new Date().toISOString(),heartbeat_age_s:2}],
  accounts:[{id:'codex-node-ui-01',node:'node-ui',provider:'codex',parallel:2,jobs:['develop','verify'],status:'idle',paused:false,
    quota_used:0.75,quota_resets_at:new Date(Date.now()+3600000).toISOString(),tokens_today:12000,cost_today:0.42,quota:{},
    machines:[{node:'node-ui',tokens_today:12000,cost_today:0.42}]},
    {id:'claude-node-ui-01',node:'node-ui',provider:'claude',parallel:1,jobs:['develop'],status:'signed_out',paused:false,
      sign_in_pending:true,quota:{}}],
  active_runs:[],recent_runs:[],projects:[{key:'TESTWP'}],tickets:[],incidents:[],events:[],totals:{},versions:{}};
S.machines.addMachine = true;
S.machines.signIns['claude-node-ui-01'] = {account:'claude-node-ui-01',sign_in_url:'https://claude.ai/login',code:'A1B2C3',expires_at:new Date(Date.now()+300000).toISOString()};
S.machines.join.command = 'curl example'; S.machines.join.expires_at = new Date(Date.now()+300000).toISOString();
renderMachines(); console.log(machinesView.innerHTML);
'''
    stubs = """var document = {addEventListener:function(){},documentElement:{setAttribute:function(){},classList:{add:function(){},remove:function(){}}},getElementById:function(){return null;},querySelector:function(){return null;},querySelectorAll:function(){return[];}};
var window = {matchMedia:function(){return {matches:false};},addEventListener:function(){}};
var location = {pathname:'/machines',origin:'https://control.example.test'};
var localStorage = {getItem:function(){return null;},setItem:function(){}}; var fetch=function(){}; var setInterval=function(){};
"""
    result = subprocess.run([node_js, "-"], input=stubs + script + driver + "})();\n",
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    html = result.stdout
    for expected in ("Machines and AI", "Memory", "Processors", "Sessions busy", "Tokens today", "Cost today",
                     "$0.420", "12k", "Stop new work", "Sessions and jobs", "Sign in again", "Sign-in pending",
                     "Finish sign-in", "A1B2C3", "data-countdown", "Add a machine", "Add AI account",
                     'data-act="open-node" data-id="node-ui">Machine details'):
        assert expected in html
    assert "Quota used" not in html and "Limit resets" not in html and "quota-history" not in html


def test_machine_join_command_waits_for_join_and_expires_after_five_minutes(client, ctx, monkeypatch, tmp_path):
    release_fixture(monkeypatch, tmp_path)
    owner_session(client, ctx)
    issued = client.post("/v1/nodes/new-console-machine/join-command", json={
        "name": "new-console-machine", "role": "worker", "project": "TESTWP"})
    assert issued.status_code == 200, issued.text
    command = issued.json()
    assert command["expires_in_s"] == 300
    assert command["command"].startswith("curl -fsSL ") and "--token" in command["command"]
    state = client.get("/v1/state").json()
    assert not any(node["name"] == "new-console-machine" and node["has_agent"] for node in state["nodes"])
    assert ctx.db.one("SELECT expires_at > now() AS valid FROM node_join_tokens WHERE node_name=%s",
                      ("new-console-machine",))["valid"] is True

    token_row = ctx.db.one("SELECT token_hash FROM node_join_tokens WHERE node_name=%s", ("new-console-machine",))
    token = shlex.split(command["command"])[-1]
    assert hashlib.sha256(token.encode()).hexdigest() == token_row["token_hash"]
    ctx.db.run("UPDATE node_join_tokens SET expires_at=now()-interval '1 second' WHERE node_name=%s",
               ("new-console-machine",))
    assert client.post("/v1/nodes/join", json={"join_token": token, "name": "new-console-machine"}).status_code == 403

    replacement = client.post("/v1/nodes/new-console-machine/join-command", json={
        "name": "new-console-machine", "role": "worker", "project": "TESTWP"})
    assert replacement.status_code == 200
    new_token = shlex.split(replacement.json()["command"])[-1]
    joined = client.post("/v1/nodes/join", json={"join_token": new_token, "name": "new-console-machine"})
    assert joined.status_code == 200
    listed = client.get("/v1/state").json()
    machine = next(node for node in listed["nodes"] if node["name"] == "new-console-machine")
    assert machine["has_agent"] is True
    assert {row["action"] for row in ctx.db.all(
        "SELECT action FROM write_audit WHERE target='node' AND ticket='new-console-machine'")} >= {"join_command"}


def test_stopped_live_machine_cannot_be_reenrolled_with_the_same_name(client, ctx, monkeypatch, tmp_path):
    release_fixture(monkeypatch, tmp_path)
    owner_session(client, ctx)
    joined = join_node(client, "screen-stopped-machine")
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    heartbeat = client.post("/v1/nodes/screen-stopped-machine/heartbeat", json={"slots_total": 8}, headers=headers)
    assert heartbeat.status_code == 200

    stopped = client.put("/v1/nodes/screen-stopped-machine/work-acceptance?accepting_work=false",
                         json={"confirm": True})
    assert stopped.status_code == 200 and stopped.json()["accepting_work"] is False
    before = ctx.db.one("SELECT token_hash,status,accepting_work FROM nodes WHERE name=%s",
                        ("screen-stopped-machine",))
    assert before["status"] == "draining" and before["accepting_work"] is False

    reenroll = client.post("/v1/nodes/screen-stopped-machine/join-command", json={
        "name": "screen-stopped-machine", "role": "worker", "project": "TESTWP"})

    assert reenroll.status_code == 409
    after = ctx.db.one("SELECT token_hash,status,accepting_work FROM nodes WHERE name=%s",
                       ("screen-stopped-machine",))
    assert after == before
    assert ctx.db.one("SELECT count(*) AS n FROM node_join_tokens WHERE node_name=%s AND used_at IS NULL",
                      ("screen-stopped-machine",))["n"] == 0


def test_ai_account_capacity_live_controls_confirmation_quota_and_audit(client, ctx, monkeypatch, tmp_path):
    owner_session(client, ctx)
    node_auth = {"Authorization": f"Bearer {join_node(client, 'node1').json()['node_token']}"}
    join_node(client, "node3")

    created = client.post("/v1/machines/node1/accounts", json={
        "provider": "codex", "sessions": 2, "jobs": ["develop", "verify", "intake", "master"]})
    assert created.status_code == 200, created.text
    account_id = created.json()["account"]
    assert created.json()["status"] == "queued" and not created.json()["sign_in_url"] and not created.json()["code"]
    assert set(created.json()["jobs"]) == {"develop", "verify", "intake", "master"}

    state = client.get("/v1/state").json()
    account = next(row for row in state["accounts"] if row["id"] == account_id)
    machine = next(row for row in state["nodes"] if row["name"] == "node1")
    assert account["status"] == "signed_out" and account["parallel"] == 2 and account["jobs"]
    assert account["sign_in_pending"] is True
    assert machine["sessions_busy"] == 0
    assert machine["metrics"]["disk_free"] == 20 * 1024 ** 3
    assert machine["metrics"]["load1"] == 0.1 and machine["metrics"]["cpus"] == 4
    assert machine["metrics"]["mem_total"] == 100 and machine["metrics"]["mem_used"] == 20

    # The machine starts the provider's own device flow and relays the provider-issued code to the console.
    again = client.post(f"/v1/accounts/{account_id}/sign-in")
    assert again.status_code == 200 and again.json()["status"] == "queued"
    requested = client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 8}, headers=node_auth)
    handoff = next(item for item in requested.json()["desired"]["sign_ins"] if item["account"] == account_id)
    assert handoff["provider"] == "codex" and handoff["request_id"]
    assert account_id not in requested.json()["desired"]["accounts"]
    from aiflow.v4.node import agent
    provider_cli = "print('Open https://auth.openai.com/codex/device and enter code ABCD-EFGH')"
    monkeypatch.setattr(agent, "ensure_account_home", lambda *_args: True)
    monkeypatch.setattr(agent, "SIGN_IN_COMMANDS", {"codex": [sys.executable, "-c", provider_cli]})
    monkeypatch.setattr(agent, "_SIGN_IN_RUN_AS", [])

    class NodeAPI:
        def call(self, method, path, body=None, **_kwargs):
            response = client.request(method, path, json=body, headers=node_auth)
            if not response.is_success:
                raise RuntimeError(f"{method} {path} -> {response.status_code}")
            return response.json()

    agent.run_provider_sign_in(NodeAPI(), "node1", handoff, threading.Event(), str(tmp_path))
    details = client.get(f"/v1/accounts/{account_id}/sign-in")
    assert details.status_code == 200 and details.json()["status"] == "completed"
    assert details.json()["sign_in_url"] == "https://auth.openai.com/codex/device"
    assert details.json()["code"] == "ABCD-EFGH"
    not_ready = client.post(f"/v1/accounts/{account_id}/sign-in/complete")
    assert not_ready.status_code == 409
    desired = client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 8}, headers=node_auth)
    assert account_id in desired.json()["desired"]["accounts"]
    probe = client.post(f"/v1/accounts/{account_id}/probe", json={"ok": True, "quota": {
        "windows": {"five_hour": {"utilization": 0.4, "resets_at": 1792561800},
                    "seven_day": {"utilization": 0.75, "resets_at": 1792638000}}}}, headers=node_auth)
    assert probe.status_code == 200
    completed = client.post(f"/v1/accounts/{account_id}/sign-in/complete")
    assert completed.status_code == 200 and completed.json()["signed_in"] is True

    usage_run = "89e035a2-2dcb-4791-b482-607d43883f5f"
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
                "started_at,ended_at,tokens_in,tokens_out,cost_usd) VALUES (%s,'TESTWP-238','TESTWP','dev',"
                "%s,'codex','node1',1,'finished','done',now(),now(),100,25,0.125)", (usage_run, account_id))
    active_run = "29e035a2-2dcb-4791-b482-607d43883f5f"
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,started_at)"
                " VALUES (%s,'TESTWP-237','TESTWP','dev','codex-01','codex','node1',2,'running',now())",
                (active_run,))
    heartbeat = client.post("/v1/nodes/node1/heartbeat", json={
        "slots_total": 8, "slots_used": 2,
        "metrics": {"load1": 0.1, "cpus": 8, "mem_total": 8589934592,
                    "mem_used": 2147483648, "disk_total": 100 * 1024 ** 3,
                    "disk_free": 20 * 1024 ** 3}}, headers=node_auth)
    assert heartbeat.status_code == 200
    state = client.get("/v1/state").json()
    account = next(row for row in state["accounts"] if row["id"] == account_id)
    assert account["sign_in_pending"] is False
    machine = next(row for row in state["nodes"] if row["name"] == "node1")
    assert account["quota_used"] == 0.75 and account["quota_resets_at"] == 1792561800
    assert account["tokens_today"] == 125 and account["cost_today"] == 0.125
    assert machine["sessions_busy"] == 1
    assert {key: machine["metrics"][key] for key in ("mem_total", "mem_used", "cpus")} == {
        "mem_total": 8589934592, "mem_used": 2147483648, "cpus": 8}

    changed = client.patch(f"/v1/accounts/{account_id}", json={"sessions": 4, "jobs": ["develop", "master"]})
    assert changed.status_code == 200 and changed.json()["sessions"] == 4
    assert changed.json()["jobs"] == ["develop", "master"]
    oversized = client.patch(f"/v1/accounts/{account_id}", json={"sessions": 8})
    assert oversized.status_code == 422
    paused = client.patch(f"/v1/accounts/{account_id}", json={"action": "pause"})
    assert paused.status_code == 200 and paused.json()["paused"] is True
    heartbeat = client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 8}, headers=node_auth)
    assert heartbeat.status_code == 200 and account_id not in heartbeat.json()["desired"]["accounts"]
    resumed = client.patch(f"/v1/accounts/{account_id}", json={"action": "resume"})
    assert resumed.status_code == 200 and resumed.json()["paused"] is False

    # A fresh machine has an independent eight-session budget.
    full = client.post("/v1/machines/node3/accounts", json={
        "provider": "claude", "sessions": 8, "jobs": ["develop"]})
    assert full.status_code == 200
    too_many = client.post("/v1/machines/node3/accounts", json={
        "provider": "grok", "sessions": 1, "jobs": ["verify"]})
    assert too_many.status_code == 422

    denied_remove = client.request("DELETE", f"/v1/accounts/{account_id}", json={"confirm": False})
    assert denied_remove.status_code == 400
    assert client.request("DELETE", f"/v1/accounts/{account_id}", json={"confirm": True}).status_code == 200
    denied_drain = client.put("/v1/nodes/node1/work-acceptance?accepting_work=false", json={"confirm": False})
    assert denied_drain.status_code == 400
    add_ticket(ctx.db, "TESTWP-999")
    drained = client.put("/v1/nodes/node1/work-acceptance?accepting_work=false", json={"confirm": True})
    assert drained.status_code == 200 and drained.json()["accepting_work"] is False
    candidates = client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=node_auth)
    assert candidates.status_code == 200 and candidates.json()["tickets"] == []
    resumed_node = client.put("/v1/nodes/node1/work-acceptance?accepting_work=true", json={"confirm": False})
    assert resumed_node.status_code == 200
    settle_heartbeat_readiness(ctx, "node1")
    candidates = client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=node_auth)
    assert any(ticket["key"] == "TESTWP-999" for ticket in candidates.json()["tickets"]), json.dumps({
        "candidates": candidates.json(),
        "node": ctx.db.one("SELECT status,accepting_work,projects,dynamic,readiness FROM nodes WHERE name=%s",
                            ("node1",)),
        "account": ctx.repo().account("codex-01"),
        "ticket": ctx.db.one("SELECT key,project,status,labels FROM tickets WHERE key=%s", ("TESTWP-999",)),
    }, default=str, sort_keys=True)
    ctx.db.run("UPDATE runs SET status='finished',outcome='done',ended_at=now() WHERE id=%s", (active_run,))
    denied_machine_remove = client.request("DELETE", "/v1/nodes/node3", json={"confirm": False})
    assert denied_machine_remove.status_code == 400
    assert client.request("DELETE", "/v1/nodes/node3", json={"confirm": True}).status_code == 200
    assert not any(node["name"] == "node3" for node in client.get("/v1/state").json()["nodes"])

    audit = ctx.db.all("SELECT target,action,accepted FROM write_audit WHERE actor='owner@example.test'")
    actions = {(row["target"], row["action"]) for row in audit if row["accepted"]}
    assert {("ai-account", action) for action in ("add", "sign_in_again", "update", "pause", "resume", "remove")} <= actions
    assert {("node", action) for action in ("stop_taking_work", "resume_work", "remove")} <= actions
    assert ("ai-account", "sign_in_complete") in {(row["target"], row["action"]) for row in ctx.db.all(
        "SELECT target,action FROM write_audit")}


def test_removing_a_control_repo_account_tombstones_it_from_worker_scheduling(client, ctx):
    owner_session(client, ctx)
    node_token = join_node(client, "node1").json()["node_token"]
    removed = client.request("DELETE", "/v1/accounts/codex-01", json={"confirm": True})
    assert removed.status_code == 200
    assert ctx.repo().account("codex-01") is None
    heartbeat = client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 8},
                            headers={"Authorization": f"Bearer {node_token}"})
    assert heartbeat.status_code == 200
    assert "codex-01" not in heartbeat.json()["desired"]["accounts"]
    assert "codex-01" not in heartbeat.json()["desired"]["workers"]
    assert not any(row["id"] == "codex-01" for row in client.get("/v1/state").json()["accounts"])


def test_adding_after_removing_a_managed_account_uses_a_fresh_account_id(client, ctx):
    owner_session(client, ctx)
    join_node(client, "managed-readd")
    first = client.post("/v1/machines/managed-readd/accounts", json={
        "provider": "codex", "sessions": 1, "jobs": ["develop"]})
    assert first.status_code == 200
    removed = client.request("DELETE", f"/v1/accounts/{first.json()['account']}", json={"confirm": True})
    assert removed.status_code == 200
    second = client.post("/v1/machines/managed-readd/accounts", json={
        "provider": "codex", "sessions": 1, "jobs": ["develop"]})
    assert second.status_code == 200
    assert second.json()["account"] != first.json()["account"]


def test_machine_session_capacity_serializes_concurrent_adds_and_updates(ctx):
    from fastapi.testclient import TestClient

    first = TestClient(control_api.create_app(ctx))
    second = TestClient(control_api.create_app(ctx))
    owner_session(first, ctx)
    owner_session(second, ctx)
    join_node(first, "parallel-add")
    barrier = threading.Barrier(2)

    def add(client):
        barrier.wait()
        return client.post("/v1/machines/parallel-add/accounts", json={
            "provider": "codex", "sessions": 5, "jobs": ["develop"]})

    with ThreadPoolExecutor(max_workers=2) as pool:
        added = list(pool.map(add, (first, second)))
    assert sorted(response.status_code for response in added) == [200, 422]
    assert sum(account.parallel for account in ctx.repo().accounts_on("parallel-add")) == 5

    join_node(first, "parallel-update")
    account_ids = []
    for provider in ("codex", "grok"):
        created = first.post(f"/v1/machines/parallel-update/accounts", json={
            "provider": provider, "sessions": 2, "jobs": ["develop"]})
        assert created.status_code == 200, created.text
        account_ids.append(created.json()["account"])
    barrier = threading.Barrier(2)

    def update(client_account):
        client, account_id = client_account
        barrier.wait()
        return client.patch(f"/v1/accounts/{account_id}", json={"sessions": 5})

    with ThreadPoolExecutor(max_workers=2) as pool:
        updated = list(pool.map(update, ((first, account_ids[0]), (second, account_ids[1]))))
    assert sorted(response.status_code for response in updated) == [200, 422]
    assert sum(account.parallel for account in ctx.repo().accounts_on("parallel-update")) == 7
