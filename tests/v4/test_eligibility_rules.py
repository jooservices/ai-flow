"""Focused tests for eligibility's account, node and project rules and stage composition."""
from types import SimpleNamespace

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import system_check
from aiflow.v4.control.features.work import eligible as eligible_module
from aiflow.v4.control.features.work import eligibility_scope
from aiflow.v4.control.features.work.eligibility_candidates import (
    dev_candidates, impact_candidates, intake_candidates, post_merge_candidates, triage_candidates,
    verify_candidates,
)


def _account_deps():
    return SimpleNamespace(account_master_paused=lambda _account: False)


def test_account_rule_holds_paused_and_cooling_accounts_and_allows_a_ready_account(ctx, monkeypatch):
    account = ctx.repo().account("claude-01")
    monkeypatch.setattr(ctx.leases, "cooling", lambda _account: None)
    monkeypatch.setattr(eligibility_scope.shared_accounts, "may_take", lambda *_args: (True, ""))

    account.paused = True
    assert not eligibility_scope.account_may_work(ctx, _account_deps(), account, "cc", account.id)

    account.paused = False
    monkeypatch.setattr(ctx.leases, "cooling", lambda _account: ("quota", "over limit"))
    assert not eligibility_scope.account_may_work(ctx, _account_deps(), account, "cc", account.id)

    monkeypatch.setattr(ctx.leases, "cooling", lambda _account: None)
    assert eligibility_scope.account_may_work(ctx, _account_deps(), account, "cc", account.id)


def test_online_node_rule_holds_offline_node_and_accepting_rule_holds_paused_agent(ctx, monkeypatch):
    row = {"projects": ["TESTWP"], "dynamic": False, "tools": {}, "metrics": {}, "readiness": {}}
    monkeypatch.setattr(ctx.db, "one", lambda *_args: None)
    assert eligibility_scope.online_node_row(ctx, "cc") is None

    monkeypatch.setattr(ctx.db, "one", lambda *_args: row)
    assert eligibility_scope.online_node_row(ctx, "cc") == row
    assert not eligibility_scope.node_accepts_work({"accepting_work": False})
    assert eligibility_scope.node_accepts_work({"accepting_work": True})


@pytest.mark.parametrize("gate", ["paused", "parallel", "service", "readiness"])
def test_project_rule_holds_paused_limited_unhealthy_or_unready_project(ctx, monkeypatch, gate):
    repo = ctx.repo()
    project = repo.projects["TESTWP"]
    row = {"projects": ["TESTWP"], "dynamic": False, "tools": {}, "metrics": {"services": {}},
           "readiness": {"checked_at": "now", "machine": [], "projects": {"TESTWP": []}}}
    if gate == "paused":
        project.paused = True
    elif gate == "parallel":
        project.limits = {"parallel": 1}
        monkeypatch.setattr(ctx.db, "all", lambda *_args: [{"project": "TESTWP", "count": 1}])
    elif gate == "service":
        project.services = ["postgres"]
    else:
        row["readiness"]["projects"]["TESTWP"] = [
            {"id": "project-repository", "name": "Repository", "result": "needs_action",
             "detail": "access is blocked", "action": "restore repository access", "affects_work": True}]
        assert system_check.blocking_reasons(row["readiness"], "TESTWP", "claude-01")

    projects, _missing_runtime = eligibility_scope.open_projects(
        ctx, repo, "cc", row, "claude-01", gap_checker=lambda *_args, **_kwargs: [])

    assert projects == []


def test_project_rule_allows_a_ready_project(ctx):
    row = {"projects": ["TESTWP"], "dynamic": False, "tools": {}, "metrics": {"services": {}},
           "readiness": {"checked_at": "now", "machine": [], "projects": {"TESTWP": []}}}

    projects, _missing_runtime = eligibility_scope.open_projects(
        ctx, ctx.repo(), "cc", row, "claude-01", gap_checker=lambda *_args, **_kwargs: [])

    assert projects == ["TESTWP"]


def _manual_stages(ctx, deps, account_id, node):
    repo = ctx.repo()
    account = repo.account(account_id)
    assert eligible_module.account_may_work(ctx, deps, account, node, account_id)
    quota_at_risk = deps.quota_forecast_at_risk(account_id)
    node_row = eligible_module.online_node_row(ctx, node)
    assert node_row and eligible_module.node_accepts_work(node_row)
    projects, missing_runtime = eligible_module.open_projects(
        ctx, repo, node, node_row, account_id, gap_checker=eligible_module.toolchain_gaps,
        service_gap_checker=eligible_module.service_gaps,
        runtime_tool=eligible_module.CONTAINER_RUNTIME_TOOL)
    roles = eligible_module.work_steps.roles_for_step(account.roles or ["dev", "verify"], None)
    assert roles and projects
    scope = eligible_module.Scope(
        ctx=ctx, deps=deps, repo=repo, account=account, node=node, node_row=node_row,
        projects=projects, roles=roles, ticket_key=None, account_id=account_id,
        missing_container_runtime=missing_runtime, quota_at_risk=quota_at_risk,
        backoff_sql=eligible_module.BACKOFF_SQL, intake_backoff_sql=eligible_module.INTAKE_BACKOFF_SQL,
        now=eligible_module.utcnow, ticket_priority=eligible_module.PRIORITY,
        gap_checker=eligible_module.toolchain_gaps, runtime_tool=eligible_module.CONTAINER_RUNTIME_TOOL)
    scope.role_modes = eligible_module.role_health.modes(ctx.db, account.id, sorted(roles))
    scope.held = set(ctx.leases.held_keys())
    scope.project_priority = {key: int(getattr(project, "priority", 5) or 5)
                              for key, project in repo.projects.items()}
    tickets = (verify_candidates(scope) + post_merge_candidates(scope) + triage_candidates(scope)
               + intake_candidates(scope) + impact_candidates(scope))
    tickets.sort(key=lambda ticket: (scope.project_priority.get(ticket["project"], 5),
                                     ticket["jira_created"] or scope.now()))
    tickets += dev_candidates(scope)
    return eligible_module.finalize(scope, tickets, routing=False, capacity_routing=False)


def test_eligible_matches_the_named_stages_for_a_simple_dev_ticket(client, ctx, monkeypatch):
    joined = join_node(client, "cc")
    assert joined.status_code == 200, joined.text
    add_ticket(ctx.db, "TESTWP-RULE-DEV")
    monkeypatch.setattr(eligible_module.hotfiles, "predict", lambda *_args, **_kwargs: {"hot": False, "files": []})
    deps = SimpleNamespace(
        account_master_paused=lambda _account: False,
        quota_forecast_at_risk=lambda _account: False,
        quota_peer_accounts=lambda *_args: [],
        last_dev=lambda *_args, **_kwargs: None,
        ticket_epic=lambda _ticket: "",
        releases=lambda: None,
        route_capacity=lambda _account, _node, tickets, **_kwargs: tickets,
        eligible=lambda *_args, **_kwargs: [],
    )

    staged = _manual_stages(ctx, deps, "claude-01", "cc")
    orchestrated = eligible_module.eligible(ctx, deps, "claude-01", "cc", routing=False,
                                            capacity_routing=False)

    assert orchestrated == staged
    assert any(ticket["key"] == "TESTWP-RULE-DEV" and ticket["step"] == "dev" for ticket in orchestrated)
