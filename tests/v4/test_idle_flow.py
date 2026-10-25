"""A flow that stands still raises a critical incident with the concrete cause; a missing stack pin is reported
at once as a configuration error."""
from __future__ import annotations

import uuid

import pytest

from aiflow.v4.control.incidents import check_idle_flow, check_stack_pins, idle_minutes, idle_reasons
from aiflow.v4.control.loops import watchdog
from tests.v4.conftest import add_ticket, join_node

HEALTHY = {"git": "git 2.43", "uv": "uv 0.5", "php": "PHP 8.3.9", "composer": "Composer 2.8.4", "wp": "WP-CLI 2.12.0"}


def online_node(client, name="cc", tools=None):
    token = join_node(client, name).json()["node_token"]
    client.post(f"/v1/nodes/{name}/heartbeat", json={"tools": tools or HEALTHY},
                headers={"Authorization": f"Bearer {token}"})


def age(ctx, key, minutes):
    ctx.db.run("UPDATE tickets SET jira_updated = now() - %s * interval '1 minute', synced_at = now() WHERE key=%s",
               (minutes, key))


def run(ctx, key, step="dev", minutes_ago=1, status="finished"):
    ctx.db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, node, fence, status, started_at)"
               " VALUES (%s,%s,%s,%s,'claude-01','cc',1,%s, now() - %s * interval '1 minute')",
               (str(uuid.uuid4()), key, key.split("-")[0], step, status, minutes_ago))


def incident(ctx, kind, subject="global"):
    return ctx.db.one("SELECT severity, title, detail FROM incidents WHERE kind=%s AND subject=%s AND closed_at IS NULL",
                      (kind, subject))


@pytest.fixture
def repo(ctx):
    r = ctx.repo()
    r.projects["TESTWP"].stack = "php-8.3"
    r.versions["tools"] = {}
    return r


def test_missing_pin_is_a_critical_configuration_incident_that_names_the_pin_and_clears_when_pinned(client, ctx, repo):
    check_stack_pins(ctx)
    for tool in ("php", "composer", "wp"):
        found = incident(ctx, "config_pin", f"TESTWP:{tool}")
        assert found["severity"] == "critical" and f"tools.{tool}" in found["detail"]
    repo.versions["tools"] = {"php": "8.3", "composer": "2.8", "wp": "2.12"}
    check_stack_pins(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='config_pin' AND closed_at IS NULL")["n"] == 0


def test_idle_flow_names_the_unpinned_tools_as_the_cause(client, ctx, repo):
    online_node(client)
    add_ticket(ctx.db, "TESTWP-950")
    age(ctx, "TESTWP-950", 30)
    check_idle_flow(ctx)
    found = incident(ctx, "flow_idle")
    assert found["severity"] == "critical" and "1 tickets are ready" in found["title"]
    assert "TESTWP-950" in found["detail"] and "versions.lock has no pin for" in found["detail"]
    for tool in ("php", "composer", "wp"):
        assert tool in found["detail"]


def test_idle_flow_names_a_parked_ticket_and_its_missing_requirement(client, ctx, repo):
    online_node(client)
    add_ticket(ctx.db, "TESTWP-957")
    age(ctx, "TESTWP-957", 30)
    ctx.db.run("INSERT INTO tool_waits (ticket_key,project,requirement,machine,run_id,detail)"
               " VALUES ('TESTWP-957','TESTWP','gitleaks','cc','run-parked','MISSING-TOOL: gitleaks')")
    check_idle_flow(ctx)
    found = incident(ctx, "flow_idle")
    assert found and "parked tickets TESTWP-957 waiting for gitleaks" in found["detail"]
    assert "no cause found" not in found["detail"]


def test_idle_flow_names_backoff_quota_licence_and_project_pause(client, ctx, repo, monkeypatch):
    online_node(client)
    add_ticket(ctx.db, "TESTWP-958")
    age(ctx, "TESTWP-958", 30)
    run(ctx, "TESTWP-958", minutes_ago=30)
    ctx.db.run("UPDATE runs SET outcome='failed',ended_at=now() WHERE ticket_key='TESTWP-958'")
    ctx.db.run("UPDATE accounts SET status='cooldown',quota_at_risk=true")
    repo.projects["TESTWP"].paused = True
    from aiflow.v4.control import licensing
    monkeypatch.setattr(licensing, "snapshot", lambda *_a, **_k: {"read_only": True, "reason": "expired"})
    check_idle_flow(ctx)
    detail = incident(ctx, "flow_idle")["detail"]
    assert "retry backoff for TESTWP-958" in detail
    assert "quota cooldown/backoff" in detail and "quota forecast" in detail
    assert "licence is read-only" in detail and "TESTWP is paused" in detail


def test_ticket_whose_dev_run_changed_nothing_still_waits_before_the_next_try(client, ctx, repo):
    from aiflow.v4.control.repositories.runs import RunsRepository
    add_ticket(ctx.db, "TESTWP-960")
    run(ctx, "TESTWP-960", minutes_ago=1)
    ctx.db.run("UPDATE runs SET outcome='no_changes',ended_at=now() WHERE ticket_key='TESTWP-960'")
    assert "TESTWP-960" in RunsRepository(ctx.db).cooling_tickets("dev")


def test_idle_flow_names_version_skew(client, ctx, repo, monkeypatch):
    online_node(client)
    add_ticket(ctx.db, "TESTWP-959")
    age(ctx, "TESTWP-959", 30)
    from aiflow.v4.control import rollout
    monkeypatch.setattr(rollout, "node_compatible", lambda *_a, **_k: {
        "compatible": False, "reason": "node runs v1.0.0, behind the control plane"})
    check_idle_flow(ctx)
    assert "version skew blocks TESTWP" in incident(ctx, "flow_idle")["detail"]


def test_idle_flow_closes_when_work_starts_and_master_runs_do_not_count(client, ctx, repo):
    online_node(client)
    add_ticket(ctx.db, "TESTWP-951")
    age(ctx, "TESTWP-951", 30)
    run(ctx, "TESTWP-951", step="master", minutes_ago=1)
    check_idle_flow(ctx)
    assert incident(ctx, "flow_idle")  # a master cycle is not work
    run(ctx, "TESTWP-951", step="dev", minutes_ago=2)
    check_idle_flow(ctx)
    assert incident(ctx, "flow_idle") is None


def test_a_ticket_that_just_became_ready_and_the_policy_threshold_do_not_count_as_a_stall(client, ctx, repo):
    online_node(client)
    add_ticket(ctx.db, "TESTWP-952")
    age(ctx, "TESTWP-952", 3)
    check_idle_flow(ctx)
    assert incident(ctx, "flow_idle") is None  # ready for 3 minutes only
    age(ctx, "TESTWP-952", 30)
    repo.policy["idle_alert_minutes"] = 60
    assert idle_minutes(repo) == 60.0
    check_idle_flow(ctx)
    assert incident(ctx, "flow_idle") is None  # the policy allows an hour
    repo.policy["idle_alert_minutes"] = "soon"
    assert idle_minutes(repo) == 15.0


def test_tickets_on_hold_blocked_or_in_backoff_are_not_a_stall(client, ctx, repo):
    online_node(client)
    add_ticket(ctx.db, "TESTWP-953", labels=("aiflow-ready", "aiflow-hold"))
    add_ticket(ctx.db, "TESTWP-954", blocked_by=("TESTWP-999",))
    for key in ("TESTWP-953", "TESTWP-954"):
        age(ctx, key, 60)
    check_idle_flow(ctx)
    assert incident(ctx, "flow_idle") is None


def test_no_node_online_is_reported_as_the_cause(client, ctx, repo):
    add_ticket(ctx.db, "TESTWP-955")
    age(ctx, "TESTWP-955", 30)
    check_idle_flow(ctx)
    assert "no node is online" in incident(ctx, "flow_idle")["detail"]


def test_the_watchdog_runs_both_checks(client, ctx, repo):
    online_node(client)
    add_ticket(ctx.db, "TESTWP-956")
    age(ctx, "TESTWP-956", 30)
    watchdog(ctx)
    assert incident(ctx, "flow_idle") and incident(ctx, "config_pin", "TESTWP:php")


def test_shared_account_signed_in_on_a_secondary_machine_counts_as_ready(client, ctx, repo):
    online_node(client)
    token = join_node(client, "node1").json()["node_token"]
    client.post("/v1/nodes/node1/heartbeat", json={"tools": HEALTHY}, headers={"Authorization": f"Bearer {token}"})
    ctx.db.run("UPDATE accounts SET node='', status='idle'")
    ctx.db.run("INSERT INTO account_machines (account_id, node, status) SELECT id, 'node1', 'signed_in' FROM accounts")
    ctx.db.run("INSERT INTO account_machines (account_id, node, status) SELECT id, 'cc', 'pending' FROM accounts")
    assert not [r for r in idle_reasons(ctx, {"TESTWP"}) if "no AI account is ready" in r]
    ctx.db.run("UPDATE account_machines SET status='pending'")
    assert [r for r in idle_reasons(ctx, {"TESTWP"}) if "no AI account is ready" in r]
