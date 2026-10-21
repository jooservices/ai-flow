from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from conftest import add_ticket


MASTER_SCOPE = "master:global"


def join(client, name):
    response = client.post("/v1/nodes/join", json={"name": name, "join_token": "join-secret"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def ready(client, node, account):
    headers = join(client, node)
    response = client.post(f"/v1/nodes/{node}/heartbeat", json={
        "workers": [{"account": account, "state": "idle"}], "slots_total": 2,
    }, headers=headers)
    assert response.status_code == 200
    return headers


def master_candidate(client, headers, account):
    return client.get("/v1/master/candidate", params={"account": account}, headers=headers).json()["candidate"]


def claim_master(client, headers, account):
    return client.post("/v1/work/claim", json={"ticket": MASTER_SCOPE, "account": account, "step": "master"},
                       headers=headers)


def finish(client, headers, run, outcome="done", error_kind=""):
    assert client.post(f"/v1/runs/{run['run_id']}/result", json={"fence": run["fence"], "outcome": outcome,
                                                                   "error_kind": error_kind},
                       headers=headers).status_code == 200
    assert client.post(f"/v1/runs/{run['run_id']}/release", json={"fence": run["fence"]},
                       headers=headers).status_code == 200


def action(client, headers, run, **payload):
    return client.post("/v1/master/actions", json={"run_id": run["run_id"], "fence": run["fence"], **payload},
                       headers=headers)


def failed_flow_run(ctx, ticket, summary, *, step="dev", error_kind="infra", outcome="failed", minutes_ago=0,
                    node="node1"):
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,error_kind,summary)"
               " VALUES (%s,%s,'TESTFLOW',%s,'codex-01','codex',%s,1,'finished',%s,%s,%s)",
               (run_id, ticket, step, node, outcome, error_kind, summary))
    if minutes_ago:
        ctx.db.run("UPDATE runs SET started_at=now()-(%s * interval '1 minute'),"
                   " ended_at=now()-(%s * interval '1 minute') WHERE id=%s",
                   (minutes_ago, minutes_ago, run_id))
    return run_id


def master_context(client, headers, run):
    response = client.get("/v1/master/context", params={"run_id": run["run_id"], "fence": run["fence"]},
                          headers=headers)
    if response.status_code != 200:
        return response
    scan = client.post("/v1/master/flow-bugs", json={"run_id": run["run_id"], "fence": run["fence"]},
                       headers=headers)
    payload = response.json()
    payload["flow_bug_reports"] = scan.json().get("reports", [])
    return SimpleNamespace(status_code=response.status_code, json=lambda: payload)


def test_master_cycle_is_single_fenced_and_next_cycle_can_use_another_ready_provider(client, ctx):
    cc = ready(client, "cc", "claude-01")
    node1 = ready(client, "node1", "codex-01")
    assert master_candidate(client, cc, "claude-01")["step"] == "master"
    assert master_candidate(client, node1, "codex-01")["step"] == "master"

    first = claim_master(client, cc, "claude-01").json()
    assert first["claimed"] and first["step"] == "master" and first["provider"] == "claude"
    assert first["ticket"]["key"] == MASTER_SCOPE
    assert ctx.leases.master_holder() == f"{first['run_id']}:{first['fence']}"
    assert master_candidate(client, node1, "codex-01") is None
    assert claim_master(client, node1, "codex-01").status_code == 409

    finish(client, cc, first)
    ctx.db.run("UPDATE runs SET ended_at=now()-interval '2 minutes' WHERE id=%s", (first["run_id"],))
    assert master_candidate(client, node1, "codex-01")["step"] == "master"
    second = claim_master(client, node1, "codex-01").json()
    assert second["claimed"] and second["provider"] == "codex"
    assert second["fence"] > first["fence"]


@pytest.mark.parametrize("failure", ["cooldown", "offline"])
def test_master_failover_uses_other_provider_after_quota_or_node_lease_loss(client, ctx, failure):
    cc = ready(client, "cc", "claude-01")
    node1 = ready(client, "node1", "codex-01")
    from aiflow.v4.core.controlrepo import Account
    ctx.repo().accounts.append(Account(id="claude-02", provider="claude", node="node1", roles=["master"]))
    ctx.db.run("INSERT INTO accounts (id,provider,node,roles,status) VALUES ('claude-02','claude','node1',%s,'idle')",
               (["master"],))
    first = claim_master(client, cc, "claude-01").json()
    assert first["claimed"]

    if failure == "cooldown":
        finish(client, cc, first, outcome="quota", error_kind="quota")
        assert ctx.leases.cooling("claude-01")
        assert master_candidate(client, cc, "claude-01") is None
    else:
        ctx.leases.r.delete(f"{ctx.leases.p}lease:{MASTER_SCOPE}")
        ctx.db.run("UPDATE nodes SET status='offline' WHERE name='cc'")
        # The watchdog has observed the expired master lease and recorded the run as lost.
        ctx.db.run("UPDATE runs SET status='lost', outcome='lost', ended_at=now() WHERE id=%s", (first["run_id"],))
        assert master_candidate(client, cc, "claude-01") is None

    candidate = master_candidate(client, node1, "codex-01")
    assert candidate and candidate["step"] == "master"
    # Codex is ready, so for this cycle the other provider is preferred over another Claude account.
    assert master_candidate(client, node1, "claude-02") is None
    # The preference is time-boxed: one cycle later any ready master account may take over again.
    ctx.db.run("UPDATE runs SET ended_at=now()-interval '2 minutes' WHERE id=%s", (first["run_id"],))
    assert master_candidate(client, node1, "claude-02")["step"] == "master"
    second = claim_master(client, node1, "codex-01").json()
    assert second["claimed"] and second["provider"] == "codex"
    assert second["fence"] > first["fence"]


@pytest.mark.parametrize("failure", ["cooldown", "offline"])
@pytest.mark.parametrize("codex_down", ["cooldown", "offline"])
def test_master_failover_uses_same_provider_when_other_provider_is_unavailable(client, ctx, failure, codex_down):
    cc = ready(client, "cc", "claude-01")
    node1 = ready(client, "node1", "codex-01")
    from aiflow.v4.core.controlrepo import Account
    ctx.repo().accounts.append(Account(id="claude-02", provider="claude", node="node2", roles=["master"]))
    ctx.db.run("INSERT INTO accounts (id,provider,node,roles,status) VALUES ('claude-02','claude','node2',%s,'idle')",
               (["master"],))
    node2 = ready(client, "node2", "claude-02")
    first = claim_master(client, cc, "claude-01").json()
    assert first["claimed"]
    if failure == "cooldown":
        finish(client, cc, first, outcome="quota", error_kind="quota")
    else:
        ctx.leases.r.delete(f"{ctx.leases.p}lease:{MASTER_SCOPE}")
        ctx.db.run("UPDATE nodes SET status='offline' WHERE name='cc'")
        ctx.db.run("UPDATE runs SET status='lost', outcome='lost', ended_at=now() WHERE id=%s", (first["run_id"],))
    if codex_down == "cooldown":
        ctx.leases.cooldown("codex-01", 1200, "quota")
    else:
        ctx.db.run("UPDATE nodes SET status='offline' WHERE name='node1'")
    assert master_candidate(client, node1, "codex-01") is None
    assert master_candidate(client, cc, "claude-01") is None

    candidate = master_candidate(client, node2, "claude-02")
    assert candidate and candidate["step"] == "master"
    second = claim_master(client, node2, "claude-02").json()
    assert second["claimed"] and second["provider"] == "claude"
    assert ctx.db.one("SELECT account_id FROM runs WHERE id=%s", (second["run_id"],))["account_id"] == "claude-02"
    assert second["fence"] > first["fence"]


def test_master_candidate_requires_role_ready_account_and_online_node(client, ctx):
    cc = ready(client, "cc", "claude-01")
    assert master_candidate(client, cc, "opencode-01") is None
    ctx.db.run("UPDATE accounts SET status='signed_out' WHERE id='claude-01'")
    assert master_candidate(client, cc, "claude-01") is None
    ctx.db.run("UPDATE accounts SET status='idle' WHERE id='claude-01'")
    ctx.db.run("UPDATE nodes SET status='offline' WHERE name='cc'")
    assert master_candidate(client, cc, "claude-01") is None


@pytest.mark.parametrize("status", ["idle", "working", "probing"])
def test_master_candidate_accepts_each_ready_account_status(client, ctx, status):
    cc = ready(client, "cc", "claude-01")
    ctx.db.run("UPDATE accounts SET status=%s WHERE id='claude-01'", (status,))
    assert master_candidate(client, cc, "claude-01")["step"] == "master"


def test_master_snapshot_contains_incidents_owner_queue_all_project_clocks_offline_and_quota(client, ctx):
    cc = ready(client, "cc", "claude-01")
    node1 = ready(client, "node1", "codex-01")
    master = claim_master(client, cc, "claude-01").json()
    add_ticket(ctx.db, "TESTWP-201", status="Needs owner")
    add_ticket(ctx.db, "TESTWP-207", status="Needs owner", labels=("aiflow-hold",))
    add_ticket(ctx.db, "TESTWP-202", status="Needs owner", labels=("owner-escalated",))
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome)"
               " VALUES (%s,'TESTWP-203','TESTWP','dev','codex-01','codex','gpt-6-luna','node1',7,'running','')",
               (run_id,))
    ctx.db.run("UPDATE runs SET started_at=now()-interval '52 minutes' WHERE id=%s", (run_id,))
    other_runs = {}
    for key, minutes in (("TESTWP-204", 31), ("TESTWP-205", 46), ("TESTWP-206", 61)):
        other_run_id = str(uuid.uuid4())
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome)"
                   " VALUES (%s,%s,'TESTWP','verify','claude-01','claude','opus','cc',8,'running','')",
                   (other_run_id, key))
        ctx.db.run("UPDATE runs SET started_at=now()-(%s * interval '1 minute') WHERE id=%s", (minutes, other_run_id))
        other_runs[minutes] = other_run_id
    ctx.db.run("UPDATE nodes SET status='offline' WHERE name='node1'")
    ctx.db.run("UPDATE accounts SET status='cooldown', cooldown_until=now()+interval '20 minutes' WHERE id='codex-01'")
    ctx.leases.cooldown("codex-01", 1200, "quota")
    from aiflow.v4.control.incidents import open_incident
    open_incident(ctx, "run_slow", "TESTWP-203", "warning", "TESTWP-203 passed 45 minutes", "slow")
    open_incident(ctx, "account_quota", "codex-01", "warning", "codex is out of quota", "quota")
    open_incident(ctx, "quota_high", "codex-01:weekly", "warning", "weekly quota high", "quota high")

    response = client.get("/v1/master/context", params={"run_id": master["run_id"], "fence": master["fence"]},
                          headers=cc)
    assert response.status_code == 200
    snapshot = response.json()
    assert {ticket["key"] for ticket in snapshot["needs_owner"]} == {"TESTWP-201", "TESTWP-207"}
    clock = next(r for r in snapshot["running_clocks"] if r["run_id"] == run_id)
    assert clock["elapsed_minutes"] >= 51
    assert clock["work_minutes"] == {"target": 30, "warn": 45, "hard": 60}
    assert clock["passed"] == ["target", "warn"]
    passed = {r["run_id"]: r["passed"] for r in snapshot["running_clocks"]}
    assert passed[other_runs[31]] == ["target"]
    assert passed[other_runs[46]] == ["target", "warn"]
    assert passed[other_runs[61]] == ["target", "warn", "hard"]
    assert any(n["name"] == "node1" for n in snapshot["offline_nodes"])
    assert any(a["id"] == "codex-01" and a["cooldown"]["reason"] == "quota"
               for a in snapshot["quota_accounts"])
    assert {i["kind"] for i in snapshot["open_incidents"]} >= {"run_slow", "account_quota", "quota_high"}
    assert {i["kind"] for i in snapshot["quota_incidents"]} == {"account_quota", "quota_high"}


def test_master_actions_are_fenced_audited_and_comments_use_master_note_format(client, ctx, monkeypatch):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    add_ticket(ctx.db, "TESTWP-210")
    sent = []
    monkeypatch.setattr("aiflow.v4.control.api.notify.send", lambda *args: sent.append(args) or ["fake"])

    commented = action(client, cc, master, action="jira_comment", ticket="TESTWP-210", intent="follow up",
                       headline="status update", what="A slow run was found", why="The warn clock passed",
                       result="The team has a current status", next="Continue monitoring")
    assert commented.status_code == 200
    body = ctx.jira.comments[-1][1]
    for line in ("AI Flow · master · status update", "What:", "Why:", "Result:", "Next:",
                 f"Run: claude-01 (claude/opus) on cc · run {master['run_id'][:8]} · fence {master['fence']}"):
        assert line in body

    updated = action(client, cc, master, action="jira_update", ticket="TESTWP-210", intent="mark as reviewed",
                      fields={"Summary": "Reviewed"}, add_labels=["master-reviewed"])
    assert updated.status_code == 200 and ctx.jira.fields[-1] == ("TESTWP-210", {"Summary": "Reviewed"})
    assert ("add", "TESTWP-210", "master-reviewed") in ctx.jira.labels
    requeued = action(client, cc, master, action="requeue", ticket="TESTWP-210", intent="return to queue",
                      target_status="To Do", what="No worker owns the ticket", why="The run lease expired",
                      result="Returned to To Do", next="The next worker can continue")
    assert requeued.status_code == 200
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-210'")["status"] == "To Do"
    opened = action(client, cc, master, action="incident_open", intent="record a stalled ticket",
                    kind="ticket_stuck", subject="TESTWP-210", severity="warning", title="stalled",
                    detail="needs a follow-up")
    assert opened.status_code == 200
    closed = action(client, cc, master, action="incident_close", intent="the ticket moved again",
                    kind="ticket_stuck", subject="TESTWP-210", result="work resumed")
    assert closed.status_code == 200
    notified = action(client, cc, master, action="notify_owner", intent="owner needs a status",
                      title="Master update", detail="The ticket has been requeued")
    assert notified.status_code == 200 and sent

    audit = ctx.db.all("SELECT action, actor, run_id, fence, accepted FROM write_audit WHERE run_id=%s",
                       (master["run_id"],))
    assert {entry["action"] for entry in audit} >= {"jira_comment", "jira_update", "requeue", "incident_open",
                                                    "incident_close", "notify_owner"}
    assert all(entry["actor"] == "claude-01" and entry["fence"] == master["fence"] and entry["accepted"]
               for entry in audit)
    event = ctx.db.one("SELECT account, run_id, fence FROM events WHERE type='master.action' ORDER BY id DESC LIMIT 1")
    assert event["account"] == "claude-01" and event["run_id"] == master["run_id"] and event["fence"] == master["fence"]

    denied = action(client, cc, master, action="github_merge", ticket="TESTWP-210", intent="merge a pull request")
    assert denied.status_code == 403
    denied_audit = ctx.db.one("SELECT accepted, result FROM write_audit WHERE run_id=%s AND action='github_merge'",
                              (master["run_id"],))
    assert not denied_audit["accepted"] and "not allowed" in denied_audit["result"]
    direct = client.post("/v1/jira/TESTWP-210/comment", json={"run_id": master["run_id"], "fence": master["fence"],
                                                                "body": "bypass"}, headers=cc)
    assert direct.status_code == 403


@pytest.mark.parametrize(("action_name", "intent", "category"), [
    ("jira_update", "Change the production infrastructure by provisioning a node", "infrastructure"),
    ("jira_update", "Thay đổi hạ tầng bằng cách cấp phát một node", "infrastructure"),
    ("jira_update", "Rotate the API secret for this service", "secret"),
    ("jira_update", "Xoay vòng secret API của dịch vụ", "secret"),
    ("jira_update", "Release this build to production", "production_release"),
    ("jira_update", "Phát hành bản này lên production", "production_release"),
    ("change_infrastructure", "Do it", "infrastructure"),
    ("secret_change", "Do it", "secret"),
    ("production_release", "Ship this build", "production_release"),
])
def test_protected_master_actions_escalate_and_never_execute(client, ctx, monkeypatch, action_name, intent, category):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    add_ticket(ctx.db, "TESTWP-220")
    ctx.jira.status_of["TESTWP-220"] = "Needs owner"
    sent = []
    monkeypatch.setattr("aiflow.v4.control.api.notify.send", lambda *args: sent.append(args) or ["fake"])
    response = action(client, cc, master, action=action_name, ticket="TESTWP-220", intent=intent,
                      fields={"Release status": "approved"}, headline="protected request")

    assert response.status_code == 200 and response.json()["escalated"]
    assert category in response.json()["reason"]
    assert not ctx.jira.fields
    assert ctx.jira.status_of["TESTWP-220"] == "Needs owner"
    assert len(ctx.jira.comments) == 1 and sent
    comment = ctx.jira.comments[0][1]
    for line in ("AI Flow · master · owner decision required", "What:", "Why:", "Result:", "Next:", "Run:"):
        assert line in comment
    attempted = ctx.db.one("SELECT accepted, result FROM write_audit WHERE run_id=%s AND action=%s",
                           (master["run_id"], action_name))
    assert not attempted["accepted"] and category in attempted["result"]
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE run_id=%s AND action='master-escalation'",
                      (master["run_id"],))["n"] == 1
    event = ctx.db.one("SELECT outcome, reason FROM events WHERE type='master.action' AND run_id=%s",
                       (master["run_id"],))
    assert event["outcome"] == "escalated" and category in event["reason"]


@pytest.mark.parametrize("category", ["infrastructure", "secret", "production_release"])
def test_explicit_protected_category_escalates_even_with_harmless_intent(category):
    from aiflow.v4.control.policy import decide_master_action
    decision = decide_master_action({}, "jira_comment", "post a follow-up", category)
    assert decision.decision == "escalate" and category in decision.reason


@pytest.mark.parametrize(("action_name", "intent"), [
    ("incident_open", "open an incident for the offline node"),
    ("requeue", "requeue the ticket after the hard limit so another worker continues"),
    ("jira_comment", "ask the team to restart the node agent on node1"),
    ("notify_owner", "tell the owner the database node has been down for 20 minutes"),
    ("dev_reply", "answer the developer that the server restart is tracked in the node_down incident"),
])
def test_monitoring_follow_ups_about_nodes_servers_and_restarts_are_allowed(action_name, intent):
    from aiflow.v4.control.policy import decide_master_action
    assert decide_master_action({}, action_name, intent).decision == "allow"


def test_incident_for_offline_node_and_requeue_after_hard_limit_are_not_escalated(client, ctx, monkeypatch):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    add_ticket(ctx.db, "TESTWP-230", status="In Progress")
    sent = []
    monkeypatch.setattr("aiflow.v4.control.api.notify.send", lambda *args: sent.append(args) or ["fake"])

    opened = action(client, cc, master, action="incident_open", intent="open an incident for the offline node",
                    kind="node_down", subject="node1", severity="critical", title="node1 is offline",
                    detail="node1 server stopped sending heartbeats; restart the node agent or check the database"
                           " and network configuration")
    assert opened.status_code == 200 and not opened.json()["escalated"]
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='node_down' AND subject='node1'"
                      " AND closed_at IS NULL")["n"] == 1

    requeued = action(client, cc, master, action="requeue", ticket="TESTWP-230",
                      intent="requeue the ticket after the hard limit", target_status="To Do",
                      what="The run on node1 passed the 60 minute hard limit",
                      why="The node server went offline and the database shows no heartbeat; restart lost the run",
                      result="Returned to To Do", next="Another ready worker continues")
    assert requeued.status_code == 200 and not requeued.json()["escalated"]
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-230'")["status"] == "To Do"
    assert not ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE run_id=%s AND action='master-escalation'",
                          (master["run_id"],))["n"]


def test_master_policy_hook_denies_tool_execution_and_master_plan_parser_is_strict(client, ctx):
    from aiflow.v4.worker.master import parse_plan

    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    auth = {"Authorization": f"Bearer {master['run_token']}"}
    decision = client.post("/v1/policy/decide", json={"tool_name": "Bash", "tool_input": {"command": "echo hello"}},
                           headers=auth).json()["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "fenced master API" in decision["permissionDecisionReason"]
    assert parse_plan('{"summary":"reviewed","actions":[]}') == {"summary": "reviewed", "actions": []}
    assert parse_plan("The status looks fine.") is None
    assert parse_plan(json.dumps({"summary": "x", "actions": [{"action": "requeue"}]})) is None


def test_codex_master_runs_use_read_only_sandbox_without_network(tmp_path, monkeypatch):
    from aiflow.v4.adapters.codex import CodexAdapter
    from aiflow.v4.worker import master as worker_master
    from aiflow.v4.worker.dev import Claimed

    class FakeApi:
        def call(self, method, path, json=None, params=None):
            if (method, path) == ("GET", "/v1/master/context"):
                return {"open_incidents": [], "needs_owner": [], "running_clocks": [], "offline_nodes": [],
                        "quota_accounts": [], "quota_incidents": []}
            assert (method, path) == ("POST", "/v1/master/flow-bugs")
            return {"reports": []}

    class FakeAdapter:
        def argv(self, _spec):
            return []

        def run(self, spec, _progress, stop=None):
            self.spec = spec
            return SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=0.0, quota={}, error_kind="", reset_at=None,
                                   final_text='{"summary":"checked","actions":[]}', log_path=spec.log_path)

    adapter = FakeAdapter()
    monkeypatch.setattr(worker_master, "get_adapter", lambda _provider: adapter)
    claimed = Claimed(run_id=str(uuid.uuid4()), fence=3, run_token="test-token", account="codex-01", provider="codex",
                      model="gpt-6-luna", effort="high", ticket={"key": MASTER_SCOPE},
                      project={"node": "node1"}, identity=("bot", "bot@example.test"), step="master")
    result = worker_master.run_master(FakeApi(), claimed, str(tmp_path), "http://control.test", 30, [])

    assert result["outcome"] == "done"
    assert adapter.spec.sandbox == "read-only"
    argv = CodexAdapter().argv(adapter.spec)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert not any("network_access=true" in item or "writable_roots" in item for item in argv)


@pytest.mark.parametrize("category", ["infrastructure", "secret", "production_release"])
def test_master_worker_forwards_category_so_protected_requests_escalate(client, ctx, monkeypatch, tmp_path, category):
    from aiflow.v4.worker import master as worker_master
    from aiflow.v4.worker.client import ControlClient
    from aiflow.v4.worker.dev import Claimed

    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    add_ticket(ctx.db, "TESTWP-240")
    sent = []
    monkeypatch.setattr("aiflow.v4.control.api.notify.send", lambda *args: sent.append(args) or ["fake"])

    class Bridge:
        def request(self, method, path, json=None, params=None):
            return client.request(method, path, json=json, params=params, headers=cc)

    api = ControlClient.__new__(ControlClient)
    api.c = Bridge()
    # The intent alone matches no protected pattern; only the explicit category makes it an owner decision.
    plan = {"summary": "checked", "actions": [{"action": "jira_comment", "ticket": "TESTWP-240",
                                               "intent": "post a follow-up", "category": category,
                                               "headline": "follow-up", "what": "w", "why": "y",
                                               "result": "r", "next": "n"}]}

    class FakeAdapter:
        def argv(self, _spec):
            return []

        def run(self, spec, _progress, stop=None):
            return SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=0.0, quota={}, error_kind="", reset_at=None,
                                   final_text=json.dumps(plan), log_path=spec.log_path)

    monkeypatch.setattr(worker_master, "get_adapter", lambda _provider: FakeAdapter())
    claimed = Claimed(run_id=master["run_id"], fence=master["fence"], run_token=master["run_token"],
                      account="claude-01", provider="claude", model="opus", effort="high",
                      ticket={"key": MASTER_SCOPE}, project={"node": "cc"}, identity=("bot", "bot@example.test"),
                      step="master")
    result = worker_master.run_master(api, claimed, str(tmp_path), "http://control.test", 30, [])

    assert result["outcome"] == "done"
    assert result["details"]["escalated"] == 1 and result["details"]["refused"] == 0
    assert sent and len(ctx.jira.comments) == 1
    assert "AI Flow · master · owner decision required" in ctx.jira.comments[0][1]
    attempted = ctx.db.one("SELECT accepted, result FROM write_audit WHERE run_id=%s AND action='jira_comment'",
                           (master["run_id"],))
    assert not attempted["accepted"] and category in attempted["result"]


def test_repeated_testflow_failures_create_a_fenced_flow_bug_in_intake(client, ctx):
    cc = ready(client, "cc", "claude-01")
    ready(client, "node1", "codex-01")
    master = claim_master(client, cc, "claude-01").json()
    ctx.db.run("UPDATE nodes SET release_version='v4.2.1' WHERE name='node1'")
    failures = [
        failed_flow_run(ctx, "TESTFLOW-101", "Git refused checkout of branch TESTFLOW-101: pathspec did not match",
                        step="triage"),
        failed_flow_run(ctx, "TESTFLOW-101", "Git refused checkout of branch TESTFLOW-101: pathspec did not match",
                        step="verify"),
        failed_flow_run(ctx, "TESTFLOW-101", "Git refused checkout of branch TESTFLOW-101: pathspec did not match",
                        step="triage"),
    ]
    assert master_context(client, cc, master).json()["flow_bug_reports"] == []
    assert not getattr(ctx.jira, "created", [])  # three repeats on one ticket do not meet the two-ticket threshold

    failures.append(failed_flow_run(
        ctx, "TESTFLOW-102", "Git refused checkout of branch TESTFLOW-102: pathspec did not match", step="verify"))
    snapshot = client.get("/v1/master/context", params={"run_id": master["run_id"], "fence": master["fence"]},
                          headers=cc)
    assert snapshot.status_code == 200 and not getattr(ctx.jira, "created", [])
    scan = client.post("/v1/master/flow-bugs", json={"run_id": master["run_id"], "fence": master["fence"]},
                       headers=cc)
    assert scan.status_code == 200 and scan.json()["reports"][0]["action"] == "created"
    created = ctx.jira.created[-1]
    assert created["project"] == "TESTFLOW" and created["issuetype"] == "Bug"
    assert created["labels"] == ["flow-bug"]
    assert ctx.jira.status_of[created["key"]] == "Intake"
    for run_id in failures:
        assert run_id in created["description"]
    for required in ("Pattern:", "Nodes:", "node1", "AI Flow releases:", "v4.2.1", "Suggested fix:",
                     "aiflow/v4/worker/gitops.py", "aiflow/v4/worker/triage.py", "aiflow/v4/worker/verify.py"):
        assert required in created["description"]
    audit = ctx.db.one("SELECT actor, run_id, fence, accepted FROM write_audit WHERE action='flow_bug_create'")
    assert audit["actor"] == "claude-01" and audit["run_id"] == master["run_id"]
    assert audit["fence"] == master["fence"] and audit["accepted"]

    finish(client, cc, master)
    assert master_context(client, cc, master).status_code == 409
    assert len(ctx.jira.created) == 1


def test_repeated_signature_reuses_open_flow_bug_and_comments_only_for_new_runs(client, ctx):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    for key in ("TESTFLOW-110", "TESTFLOW-111", "TESTFLOW-110"):
        failed_flow_run(ctx, key, f"Git checkout refused for branch {key}: reference is unavailable", step="triage")
    master_context(client, cc, master)
    flow_bug = ctx.jira.created[0]["key"]

    failed_flow_run(ctx, "TESTFLOW-112", "Git checkout refused for branch TESTFLOW-112: reference is unavailable",
                    step="verify")
    failed_flow_run(ctx, "TESTFLOW-113", "Git checkout refused for branch TESTFLOW-113: reference is unavailable",
                    step="triage")
    response = master_context(client, cc, master)
    reports = response.json()["flow_bug_reports"]
    assert len(reports) == 1
    assert reports[0]["action"] == "repeated" and reports[0]["ticket"] == flow_bug
    assert reports[0]["repeat_count"] == 2 and len(reports[0]["new_run_ids"]) == 2
    assert len(ctx.jira.created) == 1
    assert len(ctx.jira.comments) == 1 and ctx.jira.comments[0][0] == flow_bug
    assert "Repeat count: 2" in ctx.jira.comments[0][1]
    assert all(run_id in ctx.jira.comments[0][1] for run_id in response.json()["flow_bug_reports"][0]["new_run_ids"])

    master_context(client, cc, master)
    assert len(ctx.jira.created) == 1 and len(ctx.jira.comments) == 1


def test_ticket_stuck_incident_open_over_30_minutes_creates_flow_bug(client, ctx):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    run_id = failed_flow_run(ctx, "TESTFLOW-120", "verify worker error: git checkout failed while preparing review",
                             step="verify", minutes_ago=45)
    from aiflow.v4.control.incidents import open_incident
    open_incident(ctx, "ticket_stuck", "TESTFLOW-120", "warning", "TESTFLOW-120 failed repeatedly",
                  "verify checkout is stuck")
    ctx.db.run("UPDATE incidents SET opened_at=now()-interval '31 minutes'"
               " WHERE kind='ticket_stuck' AND subject='TESTFLOW-120' AND closed_at IS NULL")

    response = master_context(client, cc, master)
    assert response.status_code == 200
    assert response.json()["flow_bug_reports"][0]["action"] == "created"
    issue = ctx.jira.created[0]
    assert issue["labels"] == ["flow-bug"] and ctx.jira.status_of[issue["key"]] == "Intake"
    assert run_id in issue["description"]
    assert "incident" in issue["description"] and "open over 30 minutes" in issue["description"]


@pytest.mark.parametrize("kind,subject,outcome,error_kind,summary", [
    ("account_quota", "claude-01", "quota", "quota", "account quota exhausted"),
    ("node_down", "node1", "failed", "infra", "node node1 went offline during checkout"),
    ("node_tools", "node1:TESTFLOW", "failed", "infra", "required tool composer is missing on node1"),
])
def test_quota_offline_and_missing_tool_incidents_never_create_flow_bugs(
        client, ctx, kind, subject, outcome, error_kind, summary):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    from aiflow.v4.control.incidents import open_incident
    open_incident(ctx, kind, subject, "warning", f"{kind} incident", summary)
    for key in ("TESTFLOW-130", "TESTFLOW-131", "TESTFLOW-130"):
        failed_flow_run(ctx, key, summary, error_kind=error_kind, outcome=outcome)

    response = master_context(client, cc, master)
    assert response.status_code == 200 and response.json()["flow_bug_reports"] == []
    assert not getattr(ctx.jira, "created", [])
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind=%s AND subject=%s AND closed_at IS NULL",
                      (kind, subject))["n"] == 1


def test_flow_bug_candidate_precedes_regular_ticket_when_both_are_eligible(client, ctx):
    node1 = ready(client, "node1", "codex-01")
    add_ticket(ctx.db, "TESTWP-130", status="To Do")
    add_ticket(ctx.db, "TESTWP-131", status="To Do", labels=("aiflow-ready", "flow-bug"))
    ctx.db.run("UPDATE tickets SET type='Bug' WHERE key='TESTWP-131'")

    candidates = client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=node1).json()["tickets"]
    assert candidates[0]["key"] == "TESTWP-131"
    assert candidates[0]["step"] == "dev"
