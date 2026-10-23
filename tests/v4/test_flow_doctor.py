"""JAIF-260: the minute-leased Flow doctor recovers queue stalls and records human interventions."""
from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg

from aiflow.v4 import cli
from aiflow.v4.control import escalations, flow_doctor, role_health, tooling
from aiflow.v4.control.db import J
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


def test_role_coverage_requires_two_ready_providers_and_returns_reason_and_next_step(ctx):
    online_node(ctx, "cc")
    online_node(ctx, "node1")
    account_state(ctx, "claude-01", "idle")
    account_state(ctx, "codex-01", "error")
    coverage = next(row for row in flow_doctor.role_coverage(ctx) if row["role"] == "dev")
    assert coverage["ready_count"] == 1 and coverage["provider_count"] == 1 and not coverage["healthy"]
    assert "different providers" in coverage["reason"] and "sign" in coverage["next_step"]
    assert ctx.db.one("SELECT cause FROM flow_doctor_findings WHERE subject='role:dev'")["cause"] == "no_ready_account"


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


def test_node_agent_collapses_a_session_of_commands_into_one_intervention():
    from aiflow.v4.node import agent
    events = [{"source": "shell", "actor": "ops", "action": "execute", "target": f"c{n}",
               "happened_at": f"2026-10-23T10:0{n}:00+00:00", "source_key": f"k{n}", "reason": "r"}
              for n in range(4)]
    grouped = agent._group_interventions(events)
    assert len(grouped) == 1 and grouped[0]["details"]["events"] == 4 and grouped[0]["source_key"] == "k0"


def test_tables_created_by_later_migrations_get_the_audit_trigger(ctx):
    ctx.db.run("CREATE TABLE later_table (id int)")
    try:
        ctx.db.migrate()
        with psycopg.connect(ctx.db.url, application_name="psql") as connection:
            connection.execute("INSERT INTO later_table VALUES (1)")
        row = ctx.db.one("SELECT target FROM operator_interventions WHERE source='database' AND target='later_table'")
        assert row == {"target": "later_table"}
    finally:
        ctx.db.run("DROP TABLE later_table")


def test_direct_database_writes_and_jira_changes_enter_the_intervention_ledger(ctx):
    add_ticket(ctx.db, "TESTWP-269")
    with psycopg.connect(ctx.db.url, application_name="psql") as connection:
        connection.execute("UPDATE tickets SET summary=summary WHERE key='TESTWP-269'")
    row = ctx.db.one("SELECT source,action,target,details->>'application_name' AS app"
                     " FROM operator_interventions WHERE source='database'")
    assert row == {"source": "database", "action": "update", "target": "tickets", "app": "psql"}
    issue = {"key": "TESTWP-269", "changelog": {"histories": [{"id": "history-1",
             "created": "2026-10-23T10:00:00.000+0000", "author": {"accountId": "human", "displayName": "Human"},
             "items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]}]}}
    changes = ctx.jira.operator_changes(issue, datetime(2026, 10, 23, 9, tzinfo=timezone.utc), "flow")
    assert changes[0]["source_key"] == "jira:TESTWP-269:history-1" and changes[0]["fields"] == ["status"]


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
    assert "Requires=aiflow4-node-agent.service" in worker_unit and "After=network-online.target" in worker_unit
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
