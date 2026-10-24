"""JAIF-260: the minute-leased Flow doctor recovers queue stalls and records human interventions."""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from aiflow.v4 import cli
from aiflow.v4.control import escalations, flow_doctor, role_health, tooling
from aiflow.v4.control.db import DB, J
from tests.v4.conftest import add_ticket, join_node

ROOT = Path(__file__).resolve().parents[2]


def online_node(ctx, name: str, *, tools: dict | None = None) -> None:
    ctx.db.run("INSERT INTO nodes (name,role,status,tools,metrics,projects,dynamic,last_heartbeat)"
               " VALUES (%s,'worker','online',%s,%s,%s,false,now()) ON CONFLICT (name) DO UPDATE SET"
               " status='online',tools=EXCLUDED.tools,metrics=EXCLUDED.metrics,projects=EXCLUDED.projects",
               (name, J(tools or {}), J({}), ["TESTWP"]))


def account_state(ctx, account: str, status: str) -> None:
    config = ctx.repo().account(account)
    assert config
    ctx.db.run("INSERT INTO accounts (id,provider,node,roles,status) VALUES (%s,%s,%s,%s,%s)"
               " ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status,node=EXCLUDED.node,roles=EXCLUDED.roles",
               (config.id, config.provider, config.node, config.roles, status))


def test_idle_diagnosis_records_concrete_machine_and_account_causes_and_checked_gates(ctx):
    add_ticket(ctx.db, "TESTWP-260")
    flow_doctor._diagnose_idle_work(ctx)
    findings = ctx.db.all("SELECT cause,detail,next_step FROM flow_doctor_findings WHERE subject='TESTWP-260'")
    assert {row["cause"] for row in findings} >= {"machine_error", "no_ready_account"}
    assert all(row["detail"] and row["next_step"] for row in findings)
    assert "licence" in ", ".join(flow_doctor.CHECKED)


def test_doctor_cause_mapping_keeps_specific_queue_gates_distinct():
    cases = {
        "parked tickets TESTWP-1 waiting for gitleaks": "parked_ticket",
        "retry backoff for TESTWP-2": "backoff",
        "impossible dependency on TESTWP-3": "impossible_dependency",
        "licence blocks project TESTWP": "licence",
        "version skew blocks TESTWP": "version_skew",
        "quota forecast is deferring new work": "quota",
        "TESTWP is paused in project settings": "project_pause",
        "dev role is switched off": "role_switched_off",
        "no AI account is ready": "no_ready_account",
        "no node is online": "machine_error",
        "account opencode-01 is signed out": "account_error",
    }
    assert {message: flow_doctor._cause(message) for message in cases} == cases
    assert flow_doctor._cause("No specific cause was observed. Checked: account state and machine assignment") == "unknown"


def test_unknown_idle_diagnosis_stays_unknown_with_a_ready_account_and_machine(ctx, monkeypatch):
    add_ticket(ctx.db, "TESTWP-2601")
    online_node(ctx, "cc")
    account_state(ctx, "claude-01", "idle")
    monkeypatch.setattr(flow_doctor, "idle_reasons", lambda *_args: [
        "No specific cause was observed. Checked: account state and machine assignment"])
    flow_doctor._diagnose_idle_work(ctx)
    finding = ctx.db.one("SELECT cause,detail,next_step FROM flow_doctor_findings WHERE subject='TESTWP-2601'")
    assert ctx.db.one("SELECT status FROM accounts WHERE id='claude-01'")["status"] == "idle"
    assert ctx.db.one("SELECT status FROM nodes WHERE name='cc'")["status"] == "online"
    assert finding["cause"] == "unknown" and "no_ready_account" != finding["cause"]
    assert "parked tickets and tool waits" in finding["next_step"] and "machine state" in finding["next_step"]


def test_doctor_resolves_a_wait_for_a_non_tool_and_comments_on_the_ticket(ctx):
    add_ticket(ctx.db, "TESTWP-261")
    ctx.db.run("INSERT INTO tool_waits (ticket_key,project,requirement,machine) VALUES (%s,%s,%s,%s)",
               ("TESTWP-261", "TESTWP", "banana", "cc"))
    flow_doctor._fix_tool_waits(ctx)
    wait = ctx.db.one("SELECT resolved_at,resolution FROM tool_waits WHERE ticket_key='TESTWP-261'")
    assert wait["resolved_at"] and "not a validated tool" in wait["resolution"]
    assert ctx.db.one("SELECT action FROM flow_doctor_actions WHERE ticket_key='TESTWP-261'")["action"] == "resolve_non_tool_wait"
    assert any(key == "TESTWP-261" and "wait resolved" in body for key, body in ctx.jira.comments)


def test_doctor_clears_a_wait_when_a_serving_machine_already_has_the_catalogue_tool(ctx):
    add_ticket(ctx.db, "TESTWP-262")
    online_node(ctx, "cc", tools={"gitleaks": "8.23.0"})
    ctx.db.run("INSERT INTO tool_waits (ticket_key,project,requirement,machine) VALUES (%s,%s,%s,%s)",
               ("TESTWP-262", "TESTWP", "gitleaks", "node1"))
    flow_doctor._fix_tool_waits(ctx)
    assert ctx.db.one("SELECT resolved_at,resolution FROM tool_waits WHERE ticket_key='TESTWP-262'")["resolution"] == \
        "available on cc"
    assert ctx.db.one("SELECT action FROM flow_doctor_actions WHERE ticket_key='TESTWP-262'")["action"] == \
        "resolve_available_tool_wait"


def test_doctor_requests_a_catalogue_install_through_the_next_node_heartbeat(ctx):
    add_ticket(ctx.db, "TESTWP-263")
    online_node(ctx, "cc")
    ctx.db.run("INSERT INTO tool_waits (ticket_key,project,requirement,machine) VALUES (%s,%s,%s,%s)",
               ("TESTWP-263", "TESTWP", "gitleaks", "cc"))
    flow_doctor._fix_tool_waits(ctx)
    request = tooling.install_requests(ctx, "cc", ctx.db.one("SELECT * FROM nodes WHERE name='cc'"), ctx.repo())
    assert any(item["name"] == "gitleaks" for item in request)
    assert ctx.db.one("SELECT action,outcome FROM flow_doctor_actions WHERE ticket_key='TESTWP-263'") == \
        {"action": "install_catalogue_tool", "outcome": "requested"}


def test_doctor_drops_a_dependency_on_a_backlog_ticket_and_explains_it(ctx):
    add_ticket(ctx.db, "TESTWP-265", status="Backlog")
    add_ticket(ctx.db, "TESTWP-264", blocked_by=("TESTWP-265",))
    ctx.jira.links_by_ticket = {"TESTWP-264": [{"id": "42", "other": "TESTWP-265", "direction": "inward"}]}
    flow_doctor._fix_impossible_dependencies(ctx)
    assert ctx.jira.deleted_issue_links == ["42"]
    assert ctx.db.one("SELECT blocked_by FROM tickets WHERE key='TESTWP-264'")["blocked_by"] == []
    assert ctx.db.one("SELECT action FROM flow_doctor_actions WHERE ticket_key='TESTWP-264'")["action"] == \
        "drop_impossible_dependency"
    assert any(key == "TESTWP-264" and "cannot complete" in body for key, body in ctx.jira.comments)


def test_doctor_retries_a_dependency_explanation_after_a_transient_jira_comment_failure(ctx, monkeypatch):
    add_ticket(ctx.db, "TESTWP-2651", status="Backlog")
    add_ticket(ctx.db, "TESTWP-2641", blocked_by=("TESTWP-2651",))
    ctx.jira.links_by_ticket = {"TESTWP-2641": [{"id": "43", "other": "TESTWP-2651", "direction": "inward"}]}
    comment = ctx.jira.comment
    calls = []

    def fail_once(ticket, body):
        calls.append((ticket, body))
        if len(calls) == 1:
            raise RuntimeError("temporary Jira outage")
        return comment(ticket, body)

    monkeypatch.setattr(ctx.jira, "comment", fail_once)
    flow_doctor._fix_impossible_dependencies(ctx)
    pending = ctx.db.one("SELECT attempts,delivered_at FROM flow_doctor_comment_outbox"
                         " WHERE ticket_key='TESTWP-2641'")
    assert pending == {"attempts": 1, "delivered_at": None}
    assert ctx.db.one("SELECT outcome FROM flow_doctor_actions WHERE ticket_key='TESTWP-2641'") == \
        {"outcome": "comment_pending"}
    assert ctx.db.one("SELECT blocked_by FROM tickets WHERE key='TESTWP-2641'")["blocked_by"] == []
    assert not ctx.jira.comments

    flow_doctor._fix_impossible_dependencies(ctx)
    delivered = ctx.db.one("SELECT attempts,delivered_at FROM flow_doctor_comment_outbox"
                           " WHERE ticket_key='TESTWP-2641'")
    assert delivered["attempts"] == 2 and delivered["delivered_at"]
    assert ctx.db.one("SELECT outcome FROM flow_doctor_actions WHERE ticket_key='TESTWP-2641'") == \
        {"outcome": "removed"}
    assert len(ctx.jira.comments) == 1 and "dependency repaired" in ctx.jira.comments[0][1]


@pytest.mark.parametrize("machine_state", ["drained", "stale"])
def test_role_coverage_and_idle_diagnosis_ignore_drained_or_stale_machines(ctx, machine_state):
    add_ticket(ctx.db, f"TESTWP-coverage-{machine_state}")
    online_node(ctx, "cc")
    online_node(ctx, "node1")
    account_state(ctx, "claude-01", "idle")
    account_state(ctx, "codex-01", "idle")
    if machine_state == "drained":
        ctx.db.run("UPDATE nodes SET accepting_work=false WHERE name IN ('cc','node1')")
    else:
        ctx.db.run("UPDATE nodes SET last_heartbeat=now()-interval '5 minutes' WHERE name IN ('cc','node1')")

    coverage = next(row for row in flow_doctor.role_coverage(ctx, persist=False) if row["role"] == "dev")
    assert coverage["ready_accounts"] == [] and not coverage["healthy"]
    reasons = flow_doctor.idle_reasons(ctx, {"TESTWP"}, {f"TESTWP-coverage-{machine_state}"})
    assert "no node is online" in reasons
    assert "no online node serves TESTWP" in reasons


def test_role_coverage_excludes_an_account_on_a_drained_machine(ctx):
    online_node(ctx, "cc")
    online_node(ctx, "node1")
    account_state(ctx, "claude-01", "idle")
    account_state(ctx, "codex-01", "idle")
    ctx.db.run("UPDATE nodes SET accepting_work=false WHERE name='cc'")

    coverage = next(row for row in flow_doctor.role_coverage(ctx, persist=False) if row["role"] == "dev")
    assert coverage["ready_accounts"] == ["codex-01"] and not coverage["healthy"]


def test_doctor_requeues_a_ticket_after_retry_backoff_expires(ctx):
    add_ticket(ctx.db, "TESTWP-266")
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
               "started_at,heartbeat_at,ended_at) VALUES (%s,%s,'TESTWP','dev','claude-01','claude','cc',1,"
               "'failed','failed',%s,%s,%s)", (run_id, "TESTWP-266", old, old, old))
    flow_doctor._release_backoff(ctx)
    action = ctx.db.one("SELECT action,outcome FROM flow_doctor_actions WHERE ticket_key='TESTWP-266'")
    assert action == {"action": "requeue_after_backoff", "outcome": "ready"}
    assert any(key == "TESTWP-266" and "retry released" in body for key, body in ctx.jira.comments)


def test_doctor_records_release_after_no_changes_backoff_expires(ctx):
    add_ticket(ctx.db, "TESTWP-2661")
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
               "started_at,heartbeat_at,ended_at) VALUES (%s,%s,'TESTWP','dev','claude-01','claude','cc',1,"
               "'finished','no_changes',%s,%s,%s)", (run_id, "TESTWP-2661", old, old, old))
    flow_doctor._release_backoff(ctx)
    assert ctx.db.one("SELECT action,outcome FROM flow_doctor_actions WHERE ticket_key='TESTWP-2661'") == \
        {"action": "requeue_after_backoff", "outcome": "ready"}


def test_doctor_fences_and_restarts_a_stale_worker_session(ctx):
    add_ticket(ctx.db, "TESTWP-267", status="In Progress")
    online_node(ctx, "cc")
    account_state(ctx, "claude-01", "working")
    run_id = str(uuid.uuid4())
    claim = ctx.leases.claim("TESTWP-267", run_id, 300_000)
    assert claim.ok
    old = datetime.now(timezone.utc) - timedelta(minutes=3)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
               "started_at,heartbeat_at) VALUES (%s,%s,'TESTWP','dev','claude-01','claude','cc',%s,'running','',%s,%s)",
               (run_id, "TESTWP-267", claim.fence, old, old))
    flow_doctor._restart_stale_runs(ctx)
    row = ctx.db.one("SELECT status,outcome FROM runs WHERE id=%s", (run_id,))
    operation = ctx.db.one("SELECT node,action,payload->>'action' AS operation FROM node_operations")
    assert row == {"status": "lost", "outcome": "lost"}
    assert operation == {"node": "cc", "action": "restart-worker", "operation": "restart_worker"}
    assert ctx.leases.holder("TESTWP-267") is None
    assert any(key == "TESTWP-267" and "worker recovered" in body for key, body in ctx.jira.comments)


def test_doctor_routes_around_an_account_or_machine_failure(ctx):
    add_ticket(ctx.db, "TESTWP-268")
    online_node(ctx, "cc")
    online_node(ctx, "node1")
    account_state(ctx, "claude-01", "error")
    account_state(ctx, "codex-01", "idle")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,error_kind,"
               "started_at,heartbeat_at,ended_at) VALUES (%s,'TESTWP-268','TESTWP','dev','claude-01','claude','cc',1,"
               "'failed','failed','machine',%s,%s,%s)", (run_id, old, old, old))
    flow_doctor._route_around_failed_assignment(ctx)
    action = ctx.db.one("SELECT action,account_id,node,outcome FROM flow_doctor_actions WHERE ticket_key='TESTWP-268'")
    assert action == {"action": "move_ticket_to_ready_account", "account_id": "codex-01", "node": "node1",
                      "outcome": "released"}


@pytest.mark.parametrize("error_kind", ["machine", "tool"])
def test_doctor_routes_a_failed_verification_away_from_its_machine(client, ctx, error_kind):
    cc = {"Authorization": "Bearer " + join_node(client, "cc").json()["node_token"]}
    node1 = {"Authorization": "Bearer " + join_node(client, "node1").json()["node_token"]}
    add_ticket(ctx.db, "TESTWP-2681", status="In Verify")
    account_state(ctx, "claude-01", "idle")
    account_state(ctx, "codex-01", "idle")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,pr_url,"
               "started_at,heartbeat_at,ended_at) VALUES (%s,'TESTWP-2681','TESTWP','dev','opencode-01','opencode',"
               "'cc',1,'finished','done','https://github.com/jooservices/test-aiflow-wp/pull/2681',%s,%s,%s)",
               (str(uuid.uuid4()), old - timedelta(minutes=1), old - timedelta(minutes=1), old - timedelta(minutes=1)))
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,error_kind,"
               "started_at,heartbeat_at,ended_at) VALUES (%s,'TESTWP-2681','TESTWP','verify','claude-01','claude','cc',"
               "1,'failed','failed',%s,%s,%s,%s)", (str(uuid.uuid4()), error_kind, old, old, old))

    flow_doctor._route_around_failed_assignment(ctx)
    exclusion = ctx.db.one("SELECT step,account_id,node FROM flow_doctor_route_exclusions"
                           " WHERE ticket_key='TESTWP-2681'")
    assert exclusion == {"step": "verify", "account_id": "", "node": "cc"}
    assert ctx.db.one("SELECT action,account_id,node FROM flow_doctor_actions WHERE ticket_key='TESTWP-2681'") == \
        {"action": "move_ticket_to_ready_account", "account_id": "codex-01", "node": "node1"}

    def offered(account: str, headers: dict):
        response = client.get("/v1/work/candidates", params={"account": account}, headers=headers)
        assert response.status_code == 200, response.text
        return {ticket["key"] for ticket in response.json()["tickets"]}

    assert "TESTWP-2681" not in offered("claude-01", cc)
    assert "TESTWP-2681" in offered("codex-01", node1)


def test_the_failed_route_is_excluded_from_candidates_after_the_doctor_moves_the_ticket(client, ctx):
    cc = {"Authorization": "Bearer " + join_node(client, "cc").json()["node_token"]}
    node1 = {"Authorization": "Bearer " + join_node(client, "node1").json()["node_token"]}
    ctx.db.run("UPDATE nodes SET status='online',last_heartbeat=now()")
    add_ticket(ctx.db, "TESTWP-272")
    account_state(ctx, "claude-01", "idle")
    account_state(ctx, "codex-01", "idle")

    def offered(account: str, headers: dict) -> bool:
        tickets = client.get("/v1/work/candidates", params={"account": account}, headers=headers).json()["tickets"]
        return any(t["key"] == "TESTWP-272" for t in tickets)

    assert offered("claude-01", cc) and offered("codex-01", node1)
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,error_kind,"
               "started_at,heartbeat_at,ended_at) VALUES (%s,'TESTWP-272','TESTWP','dev','claude-01','claude','cc',1,"
               "'failed','failed','machine',%s,%s,%s)", (str(uuid.uuid4()), old, old, old))
    flow_doctor._route_around_failed_assignment(ctx)
    assert ctx.db.one("SELECT node FROM flow_doctor_route_exclusions WHERE ticket_key='TESTWP-272'") == {"node": "cc"}
    assert not offered("claude-01", cc)  # the failed machine is not offered the ticket again
    assert offered("codex-01", node1)
    ctx.db.run("UPDATE flow_doctor_route_exclusions SET expires_at=now()-interval '1 minute'")
    assert offered("claude-01", cc)  # an expired exclusion no longer applies


def test_candidate_routing_ignores_pre_reboot_node_state_until_a_fresh_heartbeat(client, ctx):
    joined = join_node(client, "cc")
    headers = {"Authorization": "Bearer " + joined.json()["node_token"]}
    add_ticket(ctx.db, "TESTWP-2721")
    account_state(ctx, "claude-01", "idle")
    ctx.db.run("UPDATE nodes SET status='online',last_heartbeat=now()-interval '5 minutes' WHERE name='cc'")
    def offered():
        response = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["tickets"]
    assert "TESTWP-2721" not in {ticket["key"] for ticket in offered()}
    client.post("/v1/nodes/cc/heartbeat", json={}, headers=headers)
    assert "TESTWP-2721" in {ticket["key"] for ticket in offered()}


def test_an_account_failure_excludes_the_account_not_its_machine(client, ctx):
    cc = {"Authorization": "Bearer " + join_node(client, "cc").json()["node_token"]}
    join_node(client, "node1")
    ctx.db.run("UPDATE nodes SET status='online',last_heartbeat=now()")
    add_ticket(ctx.db, "TESTWP-273")
    account_state(ctx, "claude-01", "idle")
    account_state(ctx, "codex-01", "idle")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,error_kind,"
               "started_at,heartbeat_at,ended_at) VALUES (%s,'TESTWP-273','TESTWP','dev','claude-01','claude','cc',1,"
               "'failed','failed','auth',%s,%s,%s)", (str(uuid.uuid4()), old, old, old))
    flow_doctor._route_around_failed_assignment(ctx)
    assert ctx.db.one("SELECT account_id,node FROM flow_doctor_route_exclusions") == {"account_id": "claude-01",
                                                                                      "node": ""}
    assert not [t for t in client.get("/v1/work/candidates", params={"account": "claude-01"},
                                      headers=cc).json()["tickets"] if t["key"] == "TESTWP-273"]


def test_doctor_retries_a_failed_managed_account_signin(ctx):
    ctx.db.run("INSERT INTO managed_ai_accounts (id,provider,node,roles,sign_in_status)"
               " VALUES ('managed-opencode','opencode','cc',%s,'failed')", (["dev"],))
    ctx.db.run("INSERT INTO accounts (id,provider,node,roles,status)"
               " VALUES ('managed-opencode','opencode','cc',%s,'error')", (["dev"],))
    flow_doctor._retry_signins(ctx)
    row = ctx.db.one("SELECT sign_in_request_id,sign_in_status FROM managed_ai_accounts WHERE id='managed-opencode'")
    assert row["sign_in_request_id"] and row["sign_in_status"] == "queued"
    assert ctx.db.one("SELECT action,outcome FROM flow_doctor_actions WHERE account_id='managed-opencode'") == \
        {"action": "retry_account_sign_in", "outcome": "queued"}


def test_doctor_replaces_interactive_signin_with_ready_accounts_without_a_person(ctx):
    online_node(ctx, "cc")
    online_node(ctx, "node1")
    ctx.repo().account("opencode-01").explicit_only = False
    account_state(ctx, "claude-01", "signed_out")
    account_state(ctx, "codex-01", "idle")
    account_state(ctx, "opencode-01", "idle")
    request_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO managed_ai_accounts (id,provider,node,roles,sign_in_request_id,sign_in_status,"
               "sign_in_url,sign_in_code,sign_in_started_at)"
               " VALUES ('claude-01','claude','cc',%s,%s,'waiting','https://auth.example.test/device','ABCD-1234',now())",
               (["dev"], request_id))

    flow_doctor._retry_signins(ctx)
    flow_doctor._retry_signins(ctx)  # don't reopen the same interactive request on the next doctor pass

    sign_in = ctx.db.one("SELECT sign_in_status,sign_in_url,sign_in_code,sign_in_error"
                         " FROM managed_ai_accounts WHERE id='claude-01'")
    assert sign_in["sign_in_status"] == "failed" and not sign_in["sign_in_url"] and not sign_in["sign_in_code"]
    assert "interactive" in sign_in["sign_in_error"]
    action = ctx.db.one("SELECT action,outcome,detail FROM flow_doctor_actions WHERE account_id='claude-01'")
    assert action["action"] == "replace_interactive_signin" and action["outcome"] == "replaced"
    assert "codex-01" in action["detail"] or "opencode-01" in action["detail"]
    coverage = next(row for row in flow_doctor.role_coverage(ctx, persist=False) if row["role"] == "dev")
    assert coverage["healthy"] and coverage["provider_count"] == 2


def test_successful_managed_signin_probe_completes_recovery_without_console_confirmation(client, ctx):
    request_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO managed_ai_accounts (id,provider,node,roles,sign_in_request_id,sign_in_status,"
               "sign_in_started_at) VALUES ('managed-opencode','opencode','cc',%s,%s,'waiting',now()-interval '1 minute')",
               (["dev"], request_id))
    ctx.db.run("INSERT INTO accounts (id,provider,node,roles,status)"
               " VALUES ('managed-opencode','opencode','cc',%s,'signed_out')", (["dev"],))
    joined = join_node(client, "cc")
    headers = {"Authorization": "Bearer " + joined.json()["node_token"]}
    waiting = client.post("/v1/nodes/cc/heartbeat", json={}, headers=headers).json()["desired"]
    assert any(row["account"] == "managed-opencode" for row in waiting["sign_ins"])
    completed = client.post("/v1/accounts/managed-opencode/sign-in/status", headers=headers,
                            json={"request_id": request_id, "status": "completed"})
    assert completed.status_code == 200, completed.text
    ready_to_probe = client.post("/v1/nodes/cc/heartbeat", json={}, headers=headers).json()["desired"]
    assert "managed-opencode" in ready_to_probe["workers"]
    probe = client.post("/v1/accounts/managed-opencode/probe", headers=headers,
                        json={"ok": True, "model": "opencode-test"})
    assert probe.status_code == 200, probe.text
    state = ctx.db.one("SELECT a.status,m.sign_in_status,m.sign_in_request_id,m.sign_in_started_at"
                       " FROM accounts a JOIN managed_ai_accounts m ON m.id=a.id WHERE a.id='managed-opencode'")
    assert state == {"status": "idle", "sign_in_status": "idle", "sign_in_request_id": "",
                     "sign_in_started_at": None}
    add_ticket(ctx.db, "TESTWP-2722")
    candidates = client.get("/v1/work/candidates", params={"account": "managed-opencode"}, headers=headers)
    assert "TESTWP-2722" in {ticket["key"] for ticket in candidates.json()["tickets"]}


def test_role_coverage_requires_two_ready_providers_and_returns_reason_and_next_step(ctx):
    online_node(ctx, "cc")
    online_node(ctx, "node1")
    account_state(ctx, "claude-01", "idle")
    account_state(ctx, "codex-01", "error")
    coverage = next(row for row in flow_doctor.role_coverage(ctx) if row["role"] == "dev")
    assert coverage["ready_count"] == 1 and coverage["provider_count"] == 1 and not coverage["healthy"]
    assert "different providers" in coverage["reason"] and "sign" in coverage["next_step"]
    assert ctx.db.one("SELECT cause FROM flow_doctor_findings WHERE subject='role:dev'")["cause"] == "no_ready_account"


def test_role_coverage_uses_routing_readiness_for_shared_accounts_pending_sign_in(ctx):
    online_node(ctx, "cc")
    online_node(ctx, "node1")
    account_state(ctx, "claude-01", "idle")
    account_state(ctx, "codex-01", "idle")

    def dev():
        return next(row for row in flow_doctor.role_coverage(ctx, persist=False) if row["role"] == "dev")

    ctx.db.run("INSERT INTO account_machines (account_id,node,status) VALUES ('codex-01','node1','pending')")
    assert dev()["ready_accounts"] == ["claude-01"] and not dev()["healthy"]  # online machine, but not signed in
    ctx.db.run("UPDATE account_machines SET status='signed_in' WHERE account_id='codex-01'")
    assert sorted(dev()["ready_accounts"]) == ["claude-01", "codex-01"] and dev()["healthy"]


@pytest.mark.parametrize("full_capacity", ["account_sessions", "machine_slots"])
def test_role_coverage_excludes_shared_accounts_at_full_routing_capacity(ctx, full_capacity):
    online_node(ctx, "cc")
    online_node(ctx, "node1")
    ctx.db.run("UPDATE nodes SET slots_total=2 WHERE name IN ('cc','node1')")
    for account_id in ("claude-01", "codex-01"):
        ctx.repo().account(account_id).session_limit = 1
        account_state(ctx, account_id, "idle")
    if full_capacity == "account_sessions":
        account_state(ctx, "claude-01", "working")
        node, account, provider = "cc", "claude-01", "claude"
    else:
        ctx.db.run("UPDATE nodes SET slots_total=1 WHERE name='node1'")
        node, account, provider = "node1", "opencode-01", "opencode"
    now = datetime.now(timezone.utc)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
               "started_at,heartbeat_at) VALUES (%s,%s,'TESTWP','dev',%s,%s,%s,1,'running','',%s,%s)",
               (str(uuid.uuid4()), f"TESTWP-coverage-{full_capacity}", account, provider, node, now, now))
    coverage = next(row for row in flow_doctor.role_coverage(ctx, persist=False) if row["role"] == "dev")
    if full_capacity == "account_sessions":
        assert coverage["ready_accounts"] == ["codex-01"]
    else:
        assert coverage["ready_accounts"] == ["claude-01"]
    assert not coverage["healthy"]


def test_doctor_expedites_a_healthy_disabled_role_through_probation(ctx):
    online_node(ctx, "cc")
    online_node(ctx, "node1")
    for account in ("claude-01", "codex-01", "opencode-01"):
        account_state(ctx, account, "idle")
    ctx.db.run("INSERT INTO role_health (account_id,role,enabled,note) VALUES ('claude-01','dev',false,'bad run')")
    coverage = next(row for row in flow_doctor.role_coverage(ctx) if row["role"] == "dev")
    assert coverage["healthy"] and "claude" in coverage["providers"]
    assert role_health.probation_slot_open(ctx.db, "claude-01", "dev")
    assert ctx.db.one("SELECT action,outcome FROM flow_doctor_actions WHERE account_id='claude-01'") == \
        {"action": "expedite_role_probation", "outcome": "available"}


def test_only_business_or_protected_irreversible_escalations_reach_a_person(ctx):
    tool = escalations.classify(ctx.db, "MISSING-TOOL: gitleaks is not installed", "TESTWP")
    technical = escalations.classify(ctx.db, "Which file defines the worker heartbeat route?", "TESTWP")
    business = escalations.classify(ctx.db, "Should the owner add another AI provider account?", "TESTWP")
    assert tool.cls == escalations.MISSING and not tool.reaches_person
    assert technical.cls == escalations.AMBIGUITY and not technical.reaches_person
    assert business.cls == escalations.BUSINESS and business.reaches_person


def test_api_and_database_actions_are_recorded_and_create_a_bug_ticket(client, ctx):
    joined = join_node(client, "cc")
    response = client.post("/v1/nodes/cc/operator-interventions", headers={
        "Authorization": f"Bearer {joined.json()['node_token']}"}, json={
            "source_key": "1792700000:42:shell", "source": "shell", "actor": "owner",
            "action": "execute", "target": "bash", "reason": "manual recovery"})
    assert response.status_code == 202, response.text
    intervention = ctx.db.one("SELECT source,actor,action,target,reason FROM operator_interventions")
    assert intervention == {"source": "shell", "actor": "owner", "action": "execute", "target": "bash",
                            "reason": "manual recovery"}
    assert flow_doctor.create_intervention_bugs(ctx) == 1
    bug = ctx.db.one("SELECT bug_ticket_key FROM operator_interventions")
    assert bug["bug_ticket_key"] and ctx.jira.created[0]["issuetype"] == "Bug"
    assert {"flow-bug", "operator-intervention"} <= set(ctx.jira.created[0]["labels"])


def test_human_console_configuration_mutation_is_an_intervention_and_creates_a_bug(client, ctx):
    from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session

    email, secret = "owner@example.test", "c" * 40
    ctx.settings.console_session_secret = secret
    ctx.db.run("INSERT INTO console_users (email,role,roles,password_hash) VALUES (%s,'administrator',%s,'test')",
               (email, ["administrator"]))
    expires = int(datetime.now(timezone.utc).timestamp()) + 3600
    client.cookies.set(COOKIE_NAME, issue_session(email, ["administrator"], expires, secret))
    changed = client.patch("/v1/accounts/claude-01", json={"sessions": 2})
    assert changed.status_code == 200, changed.text
    row = ctx.db.one("SELECT source,actor,action,target,reason FROM operator_interventions")
    assert row["source"] == "control-api" and row["actor"] == email
    assert row["action"] == "ai-account.update" and row["target"] == "claude-01"
    assert "Authenticated principal owner@example.test" in row["reason"] and "updated" in row["reason"]
    assert flow_doctor.create_intervention_bugs(ctx) == 1
    bug = ctx.db.one("SELECT bug_ticket_key FROM operator_interventions")
    assert bug["bug_ticket_key"] and ctx.jira.created[0]["issuetype"] == "Bug"


def test_one_session_of_many_commands_creates_one_bug_and_a_new_session_another(ctx):
    start = datetime.now(timezone.utc) - timedelta(hours=3)
    for n in range(5):
        flow_doctor.record_operator_intervention(ctx, source_key=f"s1:{n}", source="shell", actor="ops",
                                                 action="execute", target=f"cmd{n}",
                                                 happened_at=start + timedelta(minutes=n))
    flow_doctor.record_operator_intervention(ctx, source_key="s2:0", source="shell", actor="ops", action="execute",
                                             target="later", happened_at=start + timedelta(hours=2))
    flow_doctor.record_operator_intervention(ctx, source_key="o:0", source="shell", actor="other", action="execute",
                                             target="x", happened_at=start)
    assert flow_doctor.create_intervention_bugs(ctx) == 3
    assert len(ctx.jira.created) == 3
    assert ctx.db.one("SELECT count(*) AS n FROM operator_interventions WHERE bug_ticket_key=''")["n"] == 0


def test_events_of_one_session_across_doctor_passes_share_one_bug(ctx):
    start = datetime.now(timezone.utc) - timedelta(minutes=50)

    def record(n: int, minutes: int, actor: str = "ops") -> None:
        flow_doctor.record_operator_intervention(ctx, source_key=f"p{n}", source="shell", actor=actor,
                                                 action="execute", target=f"cmd{n}",
                                                 happened_at=start + timedelta(minutes=minutes))

    record(0, 0)
    record(1, 10)
    assert flow_doctor.create_intervention_bugs(ctx) == 1
    record(2, 25)  # a later pass: within 30 minutes of the last action of the same session
    record(3, 45)  # and again, 20 minutes after the previous one
    record(4, 30, actor="other")  # another actor is another session
    assert flow_doctor.create_intervention_bugs(ctx) == 1
    assert len(ctx.jira.created) == 2
    sessions = ctx.db.all("SELECT actor,event_count,bug_ticket_key FROM operator_intervention_sessions ORDER BY actor")
    assert [(s["actor"], s["event_count"]) for s in sessions] == [("ops", 4), ("other", 1)]
    assert len({r["bug_ticket_key"] for r in ctx.db.all("SELECT bug_ticket_key FROM operator_interventions"
                                                         " WHERE actor='ops'")}) == 1
    bug = sessions[0]["bug_ticket_key"]
    assert sum(1 for key, body in ctx.jira.comments if key == bug and "more actions" in body) == 1
    assert flow_doctor.create_intervention_bugs(ctx) == 0  # nothing new: no duplicate Bug and no repeated comment
    assert sum(1 for key, body in ctx.jira.comments if key == bug and "more actions" in body) == 1
    assert flow_doctor.operator_intervention_summary(ctx)["last_24h"] == 5
    flow_doctor.record_operator_intervention(ctx, source_key="late", source="shell", actor="ops", action="execute",
                                             target="late", happened_at=start + timedelta(hours=2, minutes=45))
    assert flow_doctor.create_intervention_bugs(ctx) == 1  # a real gap of more than 30 minutes opens a new session


def test_intervention_bugs_reach_intake_with_a_retry_and_workers_can_claim_them(ctx, control_repo):
    from fastapi.testclient import TestClient
    from aiflow.v4.control.api import create_app
    from aiflow.v4.core.controlrepo import ControlRepo
    with (control_repo / "accounts.yaml").open("a") as f:
        f.write("  - {id: grok-01, provider: grok, node: cc, tier: middle, roles: [intake_refiner]}\n")
    snap = ControlRepo.load(control_repo)
    ctx.repo = lambda: snap
    ctx.settings.maintenance_project = "TESTWP"
    app = create_app(ctx)
    app.state.sync_accounts()
    api = TestClient(app)
    api.headers.update({"Authorization": "Bearer " + join_node(api, "cc").json()["node_token"]})
    api.post("/v1/nodes/cc/heartbeat", json={})  # worker candidates require a post-reboot heartbeat
    flow_doctor.record_operator_intervention(ctx, source_key="b1", source="shell", actor="ops", action="execute",
                                             target="systemctl restart", reason="stuck")
    original = ctx.jira.transition_to
    ctx.jira.transition_to = lambda key, target: (_ for _ in ()).throw(RuntimeError("jira down"))
    assert flow_doctor.create_intervention_bugs(ctx) == 1
    key = ctx.db.one("SELECT bug_ticket_key FROM operator_intervention_sessions")["bug_ticket_key"]
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (key,))["status"] == "Created"
    assert not [t for t in api.get("/v1/work/candidates", params={"account": "grok-01"}).json()["tickets"]
                if t["key"] == key]
    ctx.jira.transition_to = original
    assert flow_doctor.create_intervention_bugs(ctx) == 0  # the retry only moves the Bug; no second Bug
    assert len(ctx.jira.created) == 1
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (key,))["status"] == "Intake"
    assert [t["step"] for t in api.get("/v1/work/candidates", params={"account": "grok-01"}).json()["tickets"]
            if t["key"] == key] == ["intake"]
    claimed = api.post("/v1/work/claim", json={"ticket": key, "account": "grok-01", "step": "intake"})
    assert claimed.status_code == 200 and claimed.json()["claimed"]


def test_node_agent_collapses_a_session_of_commands_into_one_intervention():
    from aiflow.v4.node import agent
    events = [{"source": "shell", "actor": "ops", "action": "execute", "target": f"c{n}",
               "happened_at": f"2026-10-23T10:0{n}:00+00:00", "source_key": f"k{n}", "reason": "r"}
              for n in range(4)]
    grouped = agent._group_interventions(events)
    assert len(grouped) == 1 and grouped[0]["details"]["events"] == 4 and grouped[0]["source_key"] == "k0"


def test_node_audit_reasons_name_the_observed_command_and_file_without_arguments():
    from aiflow.v4.node import agent

    raw = (b'type=SYSCALL msg=audit(1790000000.123:77): auid=1000 exe="/usr/bin/systemctl"\n'
           b'type=EXECVE msg=audit(1790000000.123:77): argc=3 a0="systemctl" a1="restart" a2="secret-value"\n'
           b'type=PATH msg=audit(1790000000.123:77): name="/etc/aiflow/node.env"\n')
    events = agent._audit_interventions(raw)
    assert len(events) == 2
    shell = next(event for event in events if event["source"] == "shell")
    config = next(event for event in events if event["source"] == "configuration")
    assert "systemctl" in shell["reason"] and "no reason was supplied" not in shell["reason"]
    assert "node.env" in config["reason"] and "no reason was supplied" not in config["reason"]
    assert "secret-value" not in repr(events)


def as_operator(ctx) -> None:
    """The test server has one login role, so make it a person's role: the registered role is the product."""
    ctx.db.run("DELETE FROM audit_product_roles")


def test_tables_created_by_later_migrations_get_the_audit_trigger(ctx):
    ctx.db.run("CREATE TABLE later_table (id int)")
    try:
        ctx.db.migrate()
        as_operator(ctx)
        with psycopg.connect(ctx.db.url, application_name="psql") as connection:
            connection.execute("INSERT INTO later_table VALUES (1)")
        row = ctx.db.one("SELECT target FROM operator_interventions WHERE source='database' AND target='later_table'")
        assert row == {"target": "later_table"}
    finally:
        ctx.db.run("DROP TABLE later_table")


def test_direct_database_writes_and_jira_changes_enter_the_intervention_ledger(ctx):
    add_ticket(ctx.db, "TESTWP-269")
    as_operator(ctx)
    with psycopg.connect(ctx.db.url, application_name="psql") as connection:
        connection.execute("UPDATE tickets SET summary=summary WHERE key='TESTWP-269'")
    row = ctx.db.one("SELECT source,action,target,reason,details->>'application_name' AS app"
                     " FROM operator_interventions WHERE source='database'")
    assert row["source"] == "database" and row["action"] == "update" and row["target"] == "tickets"
    assert row["app"] == "psql" and "issued update against table tickets" in row["reason"]
    issue = {"key": "TESTWP-269", "changelog": {"histories": [{"id": "history-1",
             "created": "2026-10-23T10:00:00.000+0000", "author": {"accountId": "human", "displayName": "Human"},
             "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]}]}}
    changes = ctx.jira.operator_changes(issue, datetime(2026, 10, 23, 9, tzinfo=timezone.utc), "flow")
    assert changes[0]["source_key"] == "jira:TESTWP-269:history-1" and changes[0]["fields"] == ["status"]


def test_the_product_role_is_not_audited_and_application_name_cannot_pose_as_the_product(ctx):
    add_ticket(ctx.db, "TESTWP-270")
    with psycopg.connect(ctx.db.url, application_name="psql") as connection:  # the product's own login role
        connection.execute("UPDATE tickets SET summary=summary WHERE key='TESTWP-270'")
    assert ctx.db.one("SELECT count(*) AS n FROM operator_interventions WHERE source='database'")["n"] == 0
    as_operator(ctx)  # a person logs in with another role and copies the product's application_name
    with psycopg.connect(ctx.db.url, application_name="aiflow4-control") as connection:
        connection.execute("UPDATE tickets SET summary=summary WHERE key='TESTWP-270'")
    row = ctx.db.one("SELECT action,target FROM operator_interventions WHERE source='database'")
    assert row == {"action": "update", "target": "tickets"}


def test_truncate_and_schema_changes_by_a_person_are_recorded(ctx):
    add_ticket(ctx.db, "TESTWP-271")
    as_operator(ctx)
    with psycopg.connect(ctx.db.url, application_name="aiflow4-control", autocommit=True) as connection:
        connection.execute("TRUNCATE tool_waits")
        connection.execute("ALTER TABLE tool_waits ADD COLUMN scratch int")
        connection.execute("DROP TRIGGER aiflow4_operator_audit ON tool_waits")
        connection.execute("CREATE TABLE hand_made (id int)")
        connection.execute("DROP TABLE hand_made")
    rows = {(r["action"], r["target"]) for r in ctx.db.all(
        "SELECT action,target FROM operator_interventions WHERE source='database'")}
    assert ("truncate", "tool_waits") in rows
    assert any(a == "alter table" and "tool_waits" in t for a, t in rows)
    assert any(a == "create table" and "hand_made" in t for a, t in rows)
    assert any(a.startswith("drop") and "hand_made" in t for a, t in rows)
    assert any(a.startswith("drop") and "aiflow4_operator_audit" in t for a, t in rows)


def test_restricted_migration_role_cannot_mark_flow_doctor_applied_without_ddl_audit(ctx, tmp_path):
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    migration = (ROOT / "aiflow/v4/control/migrations/018_flow_doctor.sql").read_text()
    setup = re.search(r"DO \$\$\nBEGIN\n    IF NOT EXISTS \(SELECT 1 FROM pg_event_trigger.*?\nEND\n\$\$;", migration, re.S)
    assert setup and "RAISE EXCEPTION" in migration and "RAISE NOTICE" not in migration
    role = "aiflow_restricted_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(ctx.db.url, autocommit=True)
    owner = admin.execute("SELECT current_user").fetchone()[0]
    restricted_db = None
    try:
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(owner)))
        admin.execute(sql.SQL("GRANT USAGE,CREATE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        admin.execute("DROP EVENT TRIGGER IF EXISTS aiflow4_operator_ddl")
        admin.execute("DROP EVENT TRIGGER IF EXISTS aiflow4_operator_drop")
        admin.execute(sql.SQL("ALTER TABLE schema_migrations OWNER TO {}").format(sql.Identifier(role)))
        directory = tmp_path / "restricted-migration"
        directory.mkdir()
        (directory / "flow_doctor_audit_setup.sql").write_text(setup.group(0))
        restricted_db = DB(make_conninfo(ctx.db.url, options=f"-c role={role}"))
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="migration role that can create PostgreSQL event triggers"):
            restricted_db.migrate(directory)
        assert ctx.db.one("SELECT 1 AS found FROM schema_migrations WHERE version='flow_doctor_audit_setup'") is None
    finally:
        if restricted_db:
            restricted_db.close()
        admin.execute(sql.SQL("ALTER TABLE schema_migrations OWNER TO {}").format(sql.Identifier(owner)))
        admin.execute(setup.group(0))
        admin.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(sql.Identifier(role)))
        admin.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(role), sql.Identifier(owner)))
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


def test_dashboard_exposes_findings_actions_role_reason_and_intervention_totals(client, ctx):
    flow_doctor._finding(ctx, "role:dev", "no_ready_account", "one provider available", "retry sign-in")
    flow_doctor.record_operator_intervention(ctx, source_key="manual-1", source="configuration", actor="owner",
                                             action="edit", target="accounts.yaml", reason="repair")
    state = client.get("/v1/state")
    assert state.status_code == 200, state.text
    data = state.json()
    assert "role_coverage" in data["flow_doctor"] and data["flow_doctor"]["findings"]
    assert data["operator_interventions"]["last_24h"] == 1
    html = (ROOT / "aiflow/v4/control/ui/console.html").read_text()
    assert "Flow doctor" in html and "Operator interventions" in html and "Last 7 days" in html


def test_control_waits_for_database_and_redis_before_migration_and_workers_follow_agent(monkeypatch):
    import redis

    calls = []
    attempts = {"db": 0}

    class Connection:
        def __enter__(self):
            calls.append("database")
            return self

        def __exit__(self, *_):
            return False

        def execute(self, query):
            calls.append(query)

    def connect(*_args, **_kwargs):
        attempts["db"] += 1
        if attempts["db"] == 1:
            raise psycopg.OperationalError("starting")
        return Connection()

    class RedisClient:
        def ping(self):
            calls.append("redis")

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_a, **_k: RedisClient())
    monkeypatch.setattr("time.sleep", lambda _: None)
    cli.wait_for_control_stores(SimpleNamespace(database_url="postgresql://db", redis_url="redis://cache"))
    assert calls == ["database", "SELECT 1", "redis"] and attempts["db"] == 2
    worker_unit = (ROOT / "deploy/v4/node/systemd/aiflow4-worker@.service").read_text()
    node_unit = (ROOT / "deploy/v4/node/systemd/aiflow4-node-agent.service").read_text()
    assert "Type=notify" in node_unit and "NotifyAccess=main" in node_unit
    assert "Requires=aiflow4-node-agent.service" in worker_unit and "After=network-online.target" in worker_unit
    from aiflow.v4.node import agent
    messages = []

    class NotifySocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def connect(self, address):
            messages.append(("address", address))

        def sendall(self, data):
            messages.append(("message", data))

    monkeypatch.setenv("NOTIFY_SOCKET", "@aiflow-test")
    monkeypatch.setattr(agent.socket, "socket", lambda *_args: NotifySocket())
    assert agent.notify_systemd("READY=1\nSTATUS=First control heartbeat succeeded")
    assert messages == [("address", "\0aiflow-test"),
                        ("message", b"READY=1\nSTATUS=First control heartbeat succeeded")]
    assert "auditd.service" in node_unit
    assert "restart: unless-stopped" in (ROOT / "deploy/v4/control/infra.yaml").read_text()
    assert "wait_for_control_stores(s)" in (ROOT / "aiflow/v4/cli.py").read_text()


def test_clean_install_enables_host_audit_and_uses_product_startup_paths():
    bootstrap = (ROOT / "deploy/v4/bin/aiflow4-bootstrap").read_text()
    node_install = (ROOT / "deploy/v4/bin/aiflow4-node-install").read_text()
    control_install = (ROOT / "aiflow/v4/install/control.py").read_text()
    audit = (ROOT / "deploy/v4/bin/aiflow4-audit-install").read_text()
    rules = (ROOT / "deploy/v4/audit/aiflow4-operator.rules").read_text()
    assert "auditd" in bootstrap and "aiflow4-audit-install" in node_install
    assert "configure_operator_audit" in control_install and "auditd" in control_install
    assert "auid!=4294967295" in rules and "aiflow4-human-config" in rules
    assert "control sync" not in audit and "database guard" not in audit
