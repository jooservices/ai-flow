from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import add_ticket, join_node


MASTER_SCOPE = "master:global"


def join(client, name):
    response = join_node(client, name)
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


def test_first_configured_senior_gets_master_role_when_control_repo_has_no_master(control_repo):
    accounts = control_repo / "accounts.yaml"
    accounts.write_text("accounts:\n  - {id: mid, provider: codex, node: node1, tier: middle, roles: [dev]}\n"
                        "  - {id: senior-a, provider: claude, node: cc, tier: senior, roles: [dev, verify]}\n"
                        "  - {id: senior-b, provider: codex, node: cc, tier: senior, roles: [dev]}\n")
    from aiflow.v4.core.controlrepo import ControlRepo
    repo = ControlRepo.load(control_repo)
    assert "master" in repo.account("senior-a").roles
    assert "master" not in repo.account("senior-b").roles
    assert "master" not in repo.account("mid").roles

    accounts.write_text("accounts:\n  - {id: senior-a, provider: claude, node: cc, tier: senior, roles: [dev]}\n"
                        "  - {id: mid, provider: codex, node: node1, tier: middle, roles: [master]}\n")
    repo = ControlRepo.load(control_repo)
    assert "master" in repo.account("mid").roles and "master" not in repo.account("senior-a").roles

    accounts.write_text("accounts:\n  - {id: senior-omitted, provider: claude, node: cc, tier: senior}\n")
    repo = ControlRepo.load(control_repo)
    assert repo.account("senior-omitted").roles == ["dev", "verify", "master"]


def test_command_center_has_master_budget_and_weekly_report_controls():
    html = (Path(__file__).parents[2] / "aiflow/v4/control/ui/console.html").read_text(encoding="utf-8")
    assert "function masterAutonomyPanel()" in html
    assert "data-master-budget" in html and "master-budget-save" in html
    assert "/v1/master/autonomy/budget" in html and "Weekly autonomy" in html


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
    assert {ticket["key"] for ticket in snapshot["needs_owner"]} == {"TESTWP-201", "TESTWP-202", "TESTWP-207"}
    assert next(ticket for ticket in snapshot["needs_owner"] if ticket["key"] == "TESTWP-202")["owner_escalated"]
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


def test_master_actions_are_limited_to_safe_fenced_actions_and_audited(client, ctx):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    add_ticket(ctx.db, "TESTWP-210")
    ctx.jira.status_of["TESTWP-210"] = "In Progress"
    moved = action(client, cc, master, action="move_to_todo", ticket="TESTWP-210",
                   intent="return the unowned ticket to To Do", why="No worker owns it")
    assert moved.status_code == 200
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-210'")["status"] == "To Do"

    add_ticket(ctx.db, "TESTWP-211")
    ctx.jira.status_of["TESTWP-211"] = "To Do"
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome)"
               " VALUES (%s,'TESTWP-211','TESTWP','dev','codex-01','codex','node1',3,'finished','failed')",
               (str(uuid.uuid4()),))
    retried = action(client, cc, master, action="retry", ticket="TESTWP-211",
                     intent="retry after the failed dev run", why="The previous run failed")
    assert retried.status_code == 200 and ctx.jira.status_of["TESTWP-211"] == "To Do"

    ctx.db.run("UPDATE accounts SET status='error',last_error='probe failed' WHERE id='codex-01'")
    paused = action(client, cc, master, action="pause_account", account_id="codex-01", pause_minutes=15,
                    intent="pause the account after its health check failed", why="probe failed")
    assert paused.status_code == 200
    assert ctx.db.one("SELECT reason FROM master_account_pauses WHERE account_id='codex-01'")

    add_ticket(ctx.db, "TESTWP-212", status="In Progress")
    ctx.jira.status_of["TESTWP-212"] = "In Progress"
    target_id = str(uuid.uuid4())
    target_lease = ctx.leases.claim("TESTWP-212", target_id, 3600000)
    assert target_lease.ok
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome)"
               " VALUES (%s,'TESTWP-212','TESTWP','dev','codex-01','codex','node1',%s,'running','')",
               (target_id, target_lease.fence))
    ctx.db.run("UPDATE runs SET started_at=now()-interval '61 minutes' WHERE id=%s", (target_id,))
    restarted = action(client, cc, master, action="restart_run", ticket="TESTWP-212", target_run_id=target_id,
                       intent="stop the run past the project hard limit", why="It exceeded 60 minutes")
    assert restarted.status_code == 200
    assert ctx.db.one("SELECT status,outcome FROM runs WHERE id=%s", (target_id,)) == {
        "status": "lost", "outcome": "lost"}
    assert not ctx.leases.holder("TESTWP-212")
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-212'")["status"] == "To Do"

    forbidden = action(client, cc, master, action="jira_comment", ticket="TESTWP-210", intent="leave a note")
    assert forbidden.status_code == 403 and not ctx.jira.comments

    audit = ctx.db.all("SELECT action, actor, run_id, fence, accepted FROM write_audit WHERE run_id=%s",
                       (master["run_id"],))
    assert {entry["action"] for entry in audit} >= {"move_to_todo", "retry", "pause_account", "restart_run",
                                                    "jira_comment"}
    assert all(entry["actor"] == "claude-01" and entry["fence"] == master["fence"] for entry in audit)
    assert all(entry["accepted"] for entry in audit if entry["action"] != "jira_comment")
    assert not next(entry for entry in audit if entry["action"] == "jira_comment")["accepted"]
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
    monkeypatch.setattr("aiflow.v4.control.api.notify.configured_channels", lambda: ["email"])
    monkeypatch.setattr("aiflow.v4.control.api.notify.send",
                        lambda *args, channels=None: sent.append(args) or ["email"])
    response = action(client, cc, master, action=action_name, ticket="TESTWP-220", intent=intent,
                      fields={"Release status": "approved"}, headline="protected request",
                      why="The request changes a protected production system", options=["Wait for owner approval",
                                                                                         "Cancel the request"],
                      observation_key="ticket:TESTWP-220:protected")

    assert response.status_code == 200 and response.json()["escalated"]
    assert "request changes a protected production system" in response.json()["reason"]
    assert not ctx.jira.fields
    assert ctx.jira.status_of["TESTWP-220"] == "Needs owner"
    assert not ctx.jira.comments and len(sent) == 1
    assert "Why a person is needed" in sent[0][2]
    assert "- Wait for owner approval" in sent[0][2]
    attempted = ctx.db.one("SELECT accepted, result FROM write_audit WHERE run_id=%s AND action=%s",
                           (master["run_id"], action_name))
    assert not attempted["accepted"] and category in attempted["result"]
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE run_id=%s AND action='master-escalation'",
                      (master["run_id"],))["n"] == 1
    event = ctx.db.one("SELECT outcome, reason FROM events WHERE type='master.action' AND run_id=%s",
                       (master["run_id"],))
    assert event["outcome"] == "escalated" and "protected production system" in event["reason"]


@pytest.mark.parametrize("category", ["infrastructure", "secret", "production_release"])
def test_explicit_protected_category_escalates_even_with_harmless_intent(category):
    from aiflow.v4.control.policy import decide_master_action
    decision = decide_master_action({}, "jira_comment", "post a follow-up", category)
    assert decision.decision == "escalate" and category in decision.reason


@pytest.mark.parametrize(("action_name", "intent"), [
    ("retry", "retry this ticket after its failed run; the offline node is only an observation"),
    ("move_to_todo", "move the ticket after the hard limit so another worker continues"),
    ("pause_account", "pause the failing account while the database node is offline"),
    ("restart_run", "restart this run after the node stopped sending heartbeats"),
    ("open_bug", "open a Bug for the eligible repeated TESTFLOW failure"),
])
def test_safe_monitoring_actions_about_nodes_and_restarts_are_allowed(action_name, intent):
    from aiflow.v4.control.policy import decide_master_action
    assert decide_master_action({}, action_name, intent).decision == "allow"


def test_move_to_todo_after_hard_limit_is_not_escalated(client, ctx, monkeypatch):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    add_ticket(ctx.db, "TESTWP-230", status="In Progress")
    ctx.jira.status_of["TESTWP-230"] = "In Progress"
    sent = []
    monkeypatch.setattr("aiflow.v4.control.api.notify.send", lambda *args: sent.append(args) or ["fake"])
    moved = action(client, cc, master, action="move_to_todo", ticket="TESTWP-230",
                   intent="move the ticket back after its run passed the hard limit",
                   why="The run exceeded the project's work-time limit on node1")
    assert moved.status_code == 200 and not moved.json()["escalated"]
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-230'")["status"] == "To Do"
    assert not sent


def test_master_escalates_one_clear_owner_message_with_options(client, ctx, monkeypatch):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    sent = []
    monkeypatch.setattr("aiflow.v4.control.api.notify.configured_channels", lambda: ["email"])
    monkeypatch.setattr("aiflow.v4.control.api.notify.send",
                        lambda *args, channels=None: sent.append((args, channels)) or ["email"])
    payload = {"action": "escalate_to_owner", "intent": "choose whether to add another provider account",
               "why": "The master cannot provision or sign in an account", "options": ["Add a Codex account", "Keep the role uncovered"],
               "observation_key": "incident:role_coverage:master"}
    first = action(client, cc, master, **payload)
    second = action(client, cc, master, **payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["escalated"] and not first.json()["already_sent"]
    assert second.json()["already_sent"] and len(sent) == 1
    assert "Why a person is needed" in sent[0][0][2]
    assert "- Add a Codex account" in sent[0][0][2] and "- Keep the role uncovered" in sent[0][0][2]
    assert sent[0][1] == ["email"]
    assert ctx.db.one("SELECT count(*) AS n FROM master_escalations")["n"] == 1


def test_master_escalation_retries_only_failed_channels(client, ctx, monkeypatch):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    attempts = []
    monkeypatch.setattr("aiflow.v4.control.api.notify.configured_channels", lambda: ["email", "slack"])

    def send(*args, channels=None):
        attempts.append(list(channels or []))
        return ["email"] if len(attempts) == 1 else ["slack"]

    monkeypatch.setattr("aiflow.v4.control.api.notify.send", send)
    payload = {"action": "escalate_to_owner", "intent": "choose whether to add another provider account",
               "why": "The master cannot provision or sign in an account", "options": ["Add a Codex account"],
               "observation_key": "incident:role_coverage:retry"}
    first = action(client, cc, master, **payload)
    second = action(client, cc, master, **payload)
    third = action(client, cc, master, **payload)

    assert first.status_code == second.status_code == third.status_code == 200
    assert attempts == [["email", "slack"], ["slack"]]
    assert first.json()["pending_channels"] == ["slack"]
    assert second.json()["sent"] == ["slack"] and second.json()["pending_channels"] == []
    assert third.json()["already_sent"] and third.json()["sent"] == []
    row = ctx.db.one("SELECT pending_channels, delivered_channels FROM master_escalations"
                     " WHERE observation_key='incident:role_coverage:retry'")
    assert row["pending_channels"] == []
    assert set(row["delivered_channels"]) == {"email", "slack"}


def test_owner_can_change_daily_budget_and_read_weekly_autonomy_report(client, ctx, monkeypatch):
    from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session

    secret = "test-console-session-secret-that-is-long-enough"
    ctx.settings.console_session_secret = secret
    client.cookies.set(COOKIE_NAME, issue_session("owner@example.test", "owner", 4_000_000_000, secret))
    initial = client.get("/v1/master/autonomy")
    assert initial.status_code == 200
    assert initial.json()["budget"]["daily_budget_minutes"] == 60
    updated = client.put("/v1/master/autonomy/budget", json={"daily_budget_minutes": 12})
    assert updated.status_code == 200 and updated.json()["budget"]["daily_budget_minutes"] == 12
    assert ctx.db.one("SELECT actor,accepted FROM write_audit WHERE action='daily-budget-update'") == {
        "actor": "owner@example.test", "accepted": True}

    add_ticket(ctx.db, "TESTWP-250", status="Done")
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome)"
               " VALUES (%s,'TESTWP-250','TESTWP','verify','codex-01','codex','node1',1,'finished','done')",
               (str(uuid.uuid4()),))
    ctx.db.run("UPDATE tickets SET done_at=now() WHERE key='TESTWP-250'")
    cc = ready(client, "cc", "claude-01")
    disabled = client.put("/v1/master/autonomy/budget", json={"daily_budget_minutes": 0})
    assert disabled.status_code == 200 and master_candidate(client, cc, "claude-01") is None
    enabled = client.put("/v1/master/autonomy/budget", json={"daily_budget_minutes": 12})
    assert enabled.status_code == 200 and master_candidate(client, cc, "claude-01")["step"] == "master"
    master = claim_master(client, cc, "claude-01").json()
    monkeypatch.setattr("aiflow.v4.control.api.notify.configured_channels", lambda: ["email"])
    monkeypatch.setattr("aiflow.v4.control.api.notify.send",
                        lambda *args, channels=None: ["email"])
    escalated = action(client, cc, master, action="escalate_to_owner", ticket="TESTWP-250",
                       intent="select how to handle the blocked decision", why="The master lacks project context",
                       options=["Ask the project owner", "Leave the ticket waiting"],
                       observation_key="ticket:TESTWP-250:needs-owner-v1")
    assert escalated.status_code == 200
    ctx.events.emit("flow_bug.created", ticket="TESTFLOW-901", project="TESTFLOW", reason="same failure")

    report = client.get("/v1/master/report?weeks=4")
    assert report.status_code == 200
    body = report.json()
    assert len(body["trend"]) == 4
    assert {account["id"] for account in body["quota_accounts"]} == {"claude-01", "codex-01", "opencode-01"}
    assert all(len(account["quota_awareness"]["history"]) == 7 for account in body["quota_accounts"])
    assert any(row["key"] == "TESTWP-250" for row in body["finished_without_person"])
    assert body["needed_person"][0]["reason"] == "The master lacks project context"
    assert body["needed_person"][0]["options"] == ["Ask the project owner", "Leave the ticket waiting"]
    assert body["trend"][-1]["finished_without_person"] == 1
    assert body["trend"][-1]["needed_person"] == 1
    assert body["trend"][-1]["repeated_failures"] == 1
    from aiflow.v4.control.db import J
    ctx.db.run("UPDATE runs SET details=%s WHERE id=%s", (J({"model_time_s": 720}), master["run_id"]))
    exhausted = client.get("/v1/master/autonomy").json()["budget"]
    assert exhausted["used_minutes"] >= 12 and exhausted["remaining_seconds"] == 0

    client.cookies.set(COOKIE_NAME, issue_session("lead@example.test", "lead", 4_000_000_000, secret))
    assert client.get("/v1/master/autonomy").status_code == 200
    assert client.put("/v1/master/autonomy/budget", json={"daily_budget_minutes": 30}).status_code == 403


def test_invalid_plan_cycles_open_incident_and_a_valid_plan_resets_the_count(client, ctx):
    cc = ready(client, "cc", "claude-01")
    counts = []
    for index in range(5):
        master = claim_master(client, cc, "claude-01").json()
        outcome = client.post("/v1/master/plan-outcome", json={"run_id": master["run_id"],
                                      "fence": master["fence"], "valid": False}, headers=cc)
        assert outcome.status_code == 200
        assert outcome.json()["invalid_streak"] == index + 1
        counts.append(outcome.json()["invalid_last_hour"])
        # The endpoint is idempotent for a cycle even if a worker retries the record request.
        duplicate = client.post("/v1/master/plan-outcome", json={"run_id": master["run_id"],
                                         "fence": master["fence"], "valid": False}, headers=cc)
        assert duplicate.json()["invalid_last_hour"] == index + 1
        finish(client, cc, master)
        ctx.db.run("UPDATE runs SET ended_at=now()-interval '2 minutes' WHERE id=%s", (master["run_id"],))
    assert counts == [1, 2, 3, 4, 5]
    incident = ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='master_invalid_plan'"
                          " AND subject='global' AND closed_at IS NULL")
    assert incident["n"] == 1

    master = claim_master(client, cc, "claude-01").json()
    valid = client.post("/v1/master/plan-outcome", json={"run_id": master["run_id"],
                                  "fence": master["fence"], "valid": True}, headers=cc)
    assert valid.status_code == 200 and valid.json()["invalid_streak"] == 0
    assert ctx.db.one("SELECT closed_at FROM incidents WHERE kind='master_invalid_plan' AND subject='global'")["closed_at"]


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


@pytest.mark.parametrize(("answers", "expected", "validity"), [
    (["not json", '{"summary":"checked","actions":[]}'], "done", [True]),
    (["not json", "still not json"], "failed", [False]),
])
def test_master_retries_malformed_plan_once_and_records_cycle_validity(tmp_path, monkeypatch, answers, expected, validity):
    from aiflow.v4.worker import master as worker_master
    from aiflow.v4.worker.dev import Claimed

    class FakeApi:
        def __init__(self):
            self.validity = []

        def call(self, method, path, json=None, params=None):
            if (method, path) == ("GET", "/v1/master/context"):
                return {"open_incidents": [{"kind": "run_slow"}], "needs_owner": [], "running_clocks": [],
                        "offline_nodes": [], "quota_accounts": [], "quota_incidents": [],
                        "autonomy": {"remaining_seconds": 600}}
            if (method, path) == ("POST", "/v1/master/flow-bugs"):
                return {"reports": []}
            if (method, path) == ("POST", "/v1/master/plan-outcome"):
                self.validity.append(json["valid"])
                return {"valid": json["valid"]}
            raise AssertionError((method, path))

    class FakeAdapter:
        def __init__(self):
            self.prompts = []
            self.answers = iter(answers)

        def argv(self, _spec):
            return []

        def run(self, spec, _progress, stop=None):
            self.prompts.append(spec.prompt)
            return SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=0.0, quota={}, error_kind="",
                                   reset_at=None, final_text=next(self.answers), log_path=spec.log_path)

    api, adapter = FakeApi(), FakeAdapter()
    monkeypatch.setattr(worker_master, "get_adapter", lambda _provider: adapter)
    claimed = Claimed(run_id=str(uuid.uuid4()), fence=3, run_token="test-token", account="claude-01",
                      provider="claude", model="opus", effort="high", ticket={"key": MASTER_SCOPE},
                      project={"node": "cc"}, identity=("bot", "bot@example.test"), step="master")
    result = worker_master.run_master(api, claimed, str(tmp_path), "http://control.test", 30, [])

    assert result["outcome"] == expected
    assert len(adapter.prompts) == 2 and "STRICT FORMAT RETRY" in adapter.prompts[1]
    assert api.validity == validity
    if expected == "failed":
        assert result["details"]["plan_attempts"] == 2
    else:
        assert result["details"]["completed"] == 0


def test_master_does_not_call_model_when_nothing_needs_review(tmp_path, monkeypatch):
    from aiflow.v4.worker import master as worker_master
    from aiflow.v4.worker.dev import Claimed

    class FakeApi:
        def call(self, method, path, json=None, params=None):
            if (method, path) == ("GET", "/v1/master/context"):
                return {"open_incidents": [], "needs_owner": [], "running_clocks": [], "offline_nodes": [],
                        "quota_accounts": [], "quota_incidents": [], "autonomy": {"remaining_seconds": 600}}
            assert (method, path) == ("POST", "/v1/master/flow-bugs")
            return {"reports": []}

    class FakeAdapter:
        def argv(self, _spec):
            return []

        def run(self, *_args, **_kwargs):
            raise AssertionError("idle master cycles must not spend a model run")

    monkeypatch.setattr(worker_master, "get_adapter", lambda _provider: FakeAdapter())
    claimed = Claimed(run_id=str(uuid.uuid4()), fence=3, run_token="test-token", account="claude-01",
                      provider="claude", model="opus", effort="high", ticket={"key": MASTER_SCOPE},
                      project={"node": "cc"}, identity=("bot", "bot@example.test"), step="master")
    result = worker_master.run_master(FakeApi(), claimed, str(tmp_path), "http://control.test", 30, [])

    assert result["outcome"] == "done" and result["details"]["no_watch"]
    assert result["details"]["model_time_s"] == 0


def test_codex_master_runs_use_read_only_sandbox_without_network(tmp_path, monkeypatch):
    from aiflow.v4.adapters.codex import CodexAdapter
    from aiflow.v4.worker import master as worker_master
    from aiflow.v4.worker.dev import Claimed

    class FakeApi:
        def call(self, method, path, json=None, params=None):
            if (method, path) == ("GET", "/v1/master/context"):
                return {"open_incidents": [{"kind": "test", "subject": "one"}], "needs_owner": [], "running_clocks": [], "offline_nodes": [],
                        "quota_accounts": [], "quota_incidents": []}
            if (method, path) == ("POST", "/v1/master/flow-bugs"):
                return {"reports": []}
            assert (method, path) == ("POST", "/v1/master/plan-outcome")
            return {"valid": json["valid"]}

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
    from aiflow.v4.control.incidents import open_incident
    open_incident(ctx, "run_slow", "TESTWP-240", "warning", "TESTWP-240 needs attention", "run passed warn")
    sent = []
    monkeypatch.setattr("aiflow.v4.control.api.notify.configured_channels", lambda: ["email"])
    monkeypatch.setattr("aiflow.v4.control.api.notify.send",
                        lambda *args, channels=None: sent.append(args) or ["email"])

    class Bridge:
        def request(self, method, path, json=None, params=None):
            return client.request(method, path, json=json, params=params, headers=cc)

    api = ControlClient.__new__(ControlClient)
    api.c = Bridge()
    # The intent alone matches no protected pattern; only the explicit category makes it an owner decision.
    plan = {"summary": "checked", "actions": [{"action": "escalate_to_owner", "ticket": "TESTWP-240",
                                               "intent": "the protected request needs an owner decision",
                                               "category": category, "why": "The request is protected",
                                               "options": ["Wait for approval", "Cancel"]}]}

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
    assert sent and not ctx.jira.comments
    attempted = ctx.db.one("SELECT accepted, result FROM write_audit WHERE run_id=%s AND action='escalate_to_owner'",
                           (master["run_id"],))
    assert attempted["accepted"] and "explicitly requested" in attempted["result"]


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


def test_open_flow_bug_comments_on_one_recurrence_after_original_runs_age_out(client, ctx):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    original_ids = [
        failed_flow_run(ctx, "TESTFLOW-117", f"Git checkout refused for branch TESTFLOW-117: ref unavailable"),
        failed_flow_run(ctx, "TESTFLOW-118", f"Git checkout refused for branch TESTFLOW-118: ref unavailable"),
        failed_flow_run(ctx, "TESTFLOW-117", f"Git checkout refused for branch TESTFLOW-117: ref unavailable"),
    ]
    created = master_context(client, cc, master).json()["flow_bug_reports"]
    assert created[0]["action"] == "created"
    flow_bug = created[0]["ticket"]

    ctx.db.run("UPDATE runs SET started_at=now()-interval '31 minutes', ended_at=now()-interval '31 minutes'"
               " WHERE id=ANY(%s::uuid[])", (original_ids,))
    recurring_id = failed_flow_run(
        ctx, "TESTFLOW-119", "Git checkout refused for branch TESTFLOW-119: ref unavailable")
    repeated = master_context(client, cc, master).json()["flow_bug_reports"]

    assert repeated == [{"action": "repeated", "ticket": flow_bug, "repeat_count": 1,
                         "new_run_ids": [recurring_id]}]
    assert len(ctx.jira.created) == 1
    assert len(ctx.jira.comments) == 1 and ctx.jira.comments[0][0] == flow_bug
    assert "Repeat count: 1" in ctx.jira.comments[0][1] and recurring_id in ctx.jira.comments[0][1]


def test_failed_intake_move_keeps_the_created_flow_bug_and_is_retried_without_a_second_issue(client, ctx):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    for key in ("TESTFLOW-115", "TESTFLOW-116", "TESTFLOW-115"):
        failed_flow_run(ctx, key, f"Git checkout refused for branch {key}: reference is unavailable", step="triage")
    real_transition = ctx.jira.transition_to
    calls = []

    def flaky_transition(key, target):
        calls.append((key, target))
        if len(calls) == 1:
            raise RuntimeError("Jira unavailable")
        return real_transition(key, target)

    ctx.jira.transition_to = flaky_transition
    first = master_context(client, cc, master).json()["flow_bug_reports"]
    assert first[0]["action"] == "created" and not first[0]["in_intake"]
    flow_bug = first[0]["ticket"]
    assert len(ctx.jira.created) == 1 and ctx.jira.status_of[flow_bug] == "Backlog"
    tracked = ctx.db.one("SELECT ticket_key, in_intake FROM flow_bug_tracker")
    assert tracked["ticket_key"] == flow_bug and not tracked["in_intake"]
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (flow_bug,))["status"] != "Intake"

    second = master_context(client, cc, master).json()["flow_bug_reports"]
    assert second == [{"action": "moved_to_intake", "ticket": flow_bug, "signature": first[0]["signature"]}]
    assert len(ctx.jira.created) == 1 and ctx.jira.status_of[flow_bug] == "Intake"
    assert ctx.db.one("SELECT in_intake FROM flow_bug_tracker")["in_intake"]
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (flow_bug,))["status"] == "Intake"

    # Once in Intake the flow may move the Bug on; the master never pulls it back to Intake.
    ctx.jira.status_of[flow_bug] = "In Progress"
    master_context(client, cc, master)
    assert len(ctx.jira.created) == 1 and len(calls) == 2


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


def test_old_ticket_stuck_incident_does_not_claim_a_failure_started_after_it_opened(client, ctx):
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    from aiflow.v4.control.incidents import open_incident
    open_incident(ctx, "ticket_stuck", "TESTFLOW-121", "warning", "TESTFLOW-121 failed repeatedly",
                  "checkout is stuck")
    ctx.db.run("UPDATE incidents SET opened_at=now()-interval '31 minutes'"
               " WHERE kind='ticket_stuck' AND subject='TESTFLOW-121' AND closed_at IS NULL")
    failed_flow_run(ctx, "TESTFLOW-121", "Git checkout refused for branch TESTFLOW-121: ref unavailable",
                    minutes_ago=10)

    response = master_context(client, cc, master)

    assert response.status_code == 200 and response.json()["flow_bug_reports"] == []
    assert not getattr(ctx.jira, "created", [])


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
