"""Characterization cases for the ordered idle-work explanations."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from _pytest.monkeypatch import MonkeyPatch

from aiflow.v4.control import incidents, licensing, rollout, tooling
from aiflow.v4.control.features.work.constants import BACKOFF_SQL


def _project(options: dict, key: str):
    values = options.get("project_options", {}).get(key, {})
    return SimpleNamespace(
        paused=values.get("paused", False),
        tools=values.get("tools", []),
        required_services=values.get("required_services", []),
        stacks=values.get("stacks", ""),
    )


class _Repo:
    def __init__(self, options: dict):
        self.options = options
        missing = options.get("missing_projects", [])
        self.projects = {key: _project(options, key) for key in options.get("projects", []) if key not in missing}
        self.versions = options.get("versions", {})

    def projects_for_node(self, _name, _projects, _dynamic):
        served = self.options.get("serves")
        if served is None:
            return list(self.projects)
        return served


class _DB:
    def __init__(self, options: dict):
        self.options = options

    def all(self, query, params=None):
        if query.startswith("SELECT name,status,accepting_work,tools,metrics,projects,dynamic,"):
            return self.options.get("nodes", [])
        if query.startswith("SELECT ticket_key,project,requirement FROM tool_waits"):
            return self.options.get("waits", [])
        if query == BACKOFF_SQL:
            step = (params or {})["step"]
            return [{"ticket_key": key} for key in self.options.get("backoff", {}).get(step, [])]
        if query.startswith("SELECT a.id, a.status, a.node, a.quota_at_risk FROM accounts a"):
            return self.options.get("accounts", [])
        if query.startswith("SELECT account_id, node, status FROM account_machines"):
            return self.options.get("machines", [])
        raise AssertionError(f"unexpected database query: {query}")


class _Context:
    def __init__(self, options: dict):
        self.options = options
        self.db = _DB(options)
        self.settings = SimpleNamespace(license_public_key="test-key")
        self.leases = SimpleNamespace(cooling=lambda account: account in options.get("lease_cooling", []))
        self._repo = _Repo(options)

    def repo(self):
        return self._repo


def _prepare(options: dict, monkeypatch: MonkeyPatch):
    accepted = options.get("accepted_requirements", [])
    monkeypatch.setattr(tooling, "requirement_is_accepted", lambda _ctx, _project, req: req in accepted)
    monkeypatch.setattr(tooling, "system_action", lambda requirement: f"install {requirement}")
    snapshot = options.get("snapshot", {"read_only": False, "reason": ""})

    def read_snapshot(*_args, **_kwargs):
        if options.get("snapshot_error"):
            raise RuntimeError("snapshot unavailable")
        return snapshot

    def project_entitlement(_ctx, project, **_kwargs):
        if options.get("project_license_error"):
            raise RuntimeError("entitlement unavailable")
        message = options.get("project_license", {}).get(project)
        if message:
            raise licensing.LicenseError(message)

    def work_entitlement(_ctx, account, _machine, **_kwargs):
        if options.get("work_license_error"):
            raise RuntimeError("work entitlement unavailable")
        message = options.get("account_license", {}).get(account)
        if message:
            raise licensing.LicenseError(message)

    monkeypatch.setattr(licensing, "snapshot", read_snapshot)
    monkeypatch.setattr(licensing, "enforce_project_entitlement", project_entitlement)
    monkeypatch.setattr(licensing, "enforce_work_entitlement", work_entitlement)

    def compatible(_ctx, node, _releases):
        result = options.get("compatibility", {}).get(node, (True, ""))
        return {"compatible": result[0], "reason": result[1]}

    monkeypatch.setattr(rollout, "node_compatible", compatible)
    monkeypatch.setattr(incidents, "toolchain_gaps", lambda tools, *_args, **_kwargs: options.get("gaps", {}).get(
        tools.get("_node", "node1"), []))
    monkeypatch.setattr(incidents, "locked_version", lambda versions, tool: versions.get("tools", {}).get(tool))
    monkeypatch.setattr(incidents, "service_gaps", lambda reported, needed: options.get(
        "node_service_gaps", {}).get((reported or {}).get("_node"), options.get("service_gaps", {})))
    monkeypatch.setattr(incidents, "normalize_stacks", lambda _stacks: options.get("normalized_stacks", []))
    monkeypatch.setattr(incidents, "STACK_TOOLS", options.get("stack_tools", incidents.STACK_TOOLS))


def _capture(options: dict) -> list[str]:
    patcher = MonkeyPatch()
    try:
        _prepare(options, patcher)
        ctx = _Context(options)
        return incidents.idle_reasons(ctx, set(options.get("projects", [])), options.get("ticket_keys"))
    finally:
        patcher.undo()


def _online(**overrides):
    values = {"name": "node1", "status": "online", "accepting_work": True, "heartbeat_fresh": True,
              "metrics": {}, "projects": [], "dynamic": False}
    values.update(overrides)
    values["tools"] = {"_node": values["name"]}
    values["metrics"] = {**values["metrics"], "services": {"_node": values["name"]}}
    return values


def _account(**overrides):
    return {"id": "account1", "status": "idle", "node": "node1", "quota_at_risk": False, **overrides}


# Expected values are literal outputs captured from the unchanged idle_reasons implementation.
CASES = [
    ("empty registry", {}, ["no node is online", "no AI account is ready (no accounts)"]),
    ("ready account and serving node", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"]}, [
        "No specific cause was observed. Checked: parked tickets and tool waits, retry backoff, project pause, licence entitlement, node availability, version skew, toolchains, test services, account status, quota, role health, machine assignment and active leases."]),
    ("node not accepting work", {"nodes": [_online(accepting_work=False)], "accounts": [_account()]}, [
        "no node is online", "no AI account is ready (account1=idle)"]),
    ("stale heartbeat", {"nodes": [_online(heartbeat_fresh=False)], "accounts": [_account()]}, [
        "no node is online", "no AI account is ready (account1=idle)"]),
    ("accepted tool wait", {"nodes": [_online()], "waits": [{"ticket_key": "TESTWP-1", "project": "TESTWP", "requirement": "gitleaks"}], "accepted_requirements": ["gitleaks"]}, [
        "parked tickets TESTWP-1 waiting for gitleaks; install gitleaks", "no AI account is ready (no accounts)"]),
    ("unaccepted tool wait", {"nodes": [_online()], "waits": [{"ticket_key": "TESTWP-1", "project": "TESTWP", "requirement": "gitleaks"}]}, [
        "no AI account is ready (no accounts)"]),
    ("wait outside requested tickets", {"nodes": [_online()], "ticket_keys": {"TESTWP-2"}, "waits": [{"ticket_key": "TESTWP-1", "project": "TESTWP", "requirement": "gitleaks"}], "accepted_requirements": ["gitleaks"]}, [
        "no AI account is ready (no accounts)"]),
    ("parked ticket display limit", {"nodes": [_online()], "waits": [{"ticket_key": f"TESTWP-{n}", "project": "TESTWP", "requirement": "gitleaks"} for n in range(1, 8)], "accepted_requirements": ["gitleaks"]}, [
        "parked tickets TESTWP-1, TESTWP-2, TESTWP-3, TESTWP-4, TESTWP-5, TESTWP-6 and 1 more waiting for gitleaks; install gitleaks", "no AI account is ready (no accounts)"]),
    ("duplicate parked rows collapse to one ticket", {"nodes": [_online()], "waits": [{"ticket_key": "TESTWP-1", "project": "TESTWP", "requirement": "gitleaks"}, {"ticket_key": "TESTWP-1", "project": "TESTWP", "requirement": "gitleaks"}], "accepted_requirements": ["gitleaks"]}, [
        "parked tickets TESTWP-1 waiting for gitleaks; install gitleaks", "no AI account is ready (no accounts)"]),
    ("backoff ticket intersection", {"nodes": [_online()], "ticket_keys": {"TESTWP-1"}, "backoff": {"dev": ["TESTWP-1"], "verify": []}}, [
        "retry backoff for TESTWP-1", "no AI account is ready (no accounts)"]),
    ("backoff combines steps and caps displayed tickets", {"nodes": [_online()], "ticket_keys": {"TESTWP-1", "TESTWP-2", "TESTWP-3", "TESTWP-4", "TESTWP-5", "TESTWP-6", "TESTWP-7", "TESTWP-8", "TESTWP-9"}, "backoff": {"dev": ["TESTWP-9", "TESTWP-1", "TESTWP-2", "TESTWP-3", "TESTWP-4"], "verify": ["TESTWP-5", "TESTWP-6", "TESTWP-7", "TESTWP-8"]}}, [
        "retry backoff for TESTWP-1, TESTWP-2, TESTWP-3, TESTWP-4, TESTWP-5, TESTWP-6, TESTWP-7, TESTWP-8", "no AI account is ready (no accounts)"]),
    ("backoff absent when no ticket keys", {"nodes": [_online()], "backoff": {"dev": ["TESTWP-1"]}}, [
        "no AI account is ready (no accounts)"]),
    ("paused project", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"], "project_options": {"TESTWP": {"paused": True}}}, [
        "TESTWP is paused in project settings"]),
    ("read-only licence", {"nodes": [_online()], "accounts": [_account()], "snapshot": {"read_only": True, "reason": "expired"}}, [
        "licence is read-only (expired)"]),
    ("project entitlement refusal", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"], "project_license": {"TESTWP": "Projects limit reached"}}, [
        "licence limit stops new work for project TESTWP: Projects limit reached"]),
    ("licence snapshot failure", {"nodes": [_online()], "accounts": [_account()], "snapshot_error": True}, [
        "licence status could not be checked (RuntimeError)"]),
    ("online node serves no requested project", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"], "serves": []}, [
        "no online node serves TESTWP"]),
    ("version skew", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"], "compatibility": {"node1": (False, "behind release")}}, [
        "version skew blocks TESTWP on node1 (behind release)"]),
    ("unlocked stack tool", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"], "project_options": {"TESTWP": {"stacks": "php-8.3"}}, "normalized_stacks": ["php-8.3"], "gaps": {"node1": ["php"]}}, [
        "TESTWP is gated on every node that serves it: node1 (versions.lock has no pin for php)"]),
    ("missing unstacked tool", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"], "gaps": {"node1": ["git"]}}, [
        "TESTWP is gated on every node that serves it: node1 (tools missing or outside their pin: git)"]),
    ("unhealthy required service", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"], "project_options": {"TESTWP": {"required_services": ["postgres"]}}, "service_gaps": {"postgres": "connection refused"}}, [
        "TESTWP is gated on every node that serves it: node1 (test services unhealthy: postgres: connection refused)"]),
    ("one compatible ungated node", {"nodes": [_online(), _online(name="node2")], "accounts": [_account()], "projects": ["TESTWP"], "gaps": {"node1": ["git"]}, "serves": ["TESTWP"]}, [
        "No specific cause was observed. Checked: parked tickets and tool waits, retry backoff, project pause, licence entitlement, node availability, version skew, toolchains, test services, account status, quota, role health, machine assignment and active leases."]),
    ("quota cooldown and no ready account", {"nodes": [_online()], "accounts": [_account(status="cooldown")]}, [
        "quota cooldown/backoff affects account1"]),
    ("quota forecast defers account", {"nodes": [_online()], "accounts": [_account(quota_at_risk=True)]}, [
        "quota forecast is deferring new work for account1"]),
    ("pending machine assignment", {"nodes": [_online()], "accounts": [_account()], "machines": [{"account_id": "account1", "node": "node1", "status": "pending"}]}, [
        "no AI account is ready (account1=idle)"]),
    ("shared signed-in assignment", {"nodes": [_online()], "accounts": [_account(node="")], "machines": [{"account_id": "account1", "node": "node1", "status": "signed_in"}]}, [
        "No specific cause was observed. Checked: parked tickets and tool waits, retry backoff, project pause, licence entitlement, node availability, version skew, toolchains, test services, account status, quota, role health, machine assignment and active leases."]),
    ("work entitlement refusal", {"nodes": [_online()], "accounts": [_account()], "account_license": {"account1": "AI account limit reached"}}, [
        "licence limit stops new work for 1 AI account(s) (account1): AI account limit reached"]),
    ("work entitlement failure", {"nodes": [_online()], "accounts": [_account()], "work_license_error": True}, [
        "licence work entitlement could not be checked (RuntimeError)"]),
    ("project entitlement failure", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"], "project_license_error": True}, [
        "licence status could not be checked (RuntimeError)"]),
    ("read-only licence without reason", {"nodes": [_online()], "accounts": [_account()], "snapshot": {"read_only": True, "reason": ""}}, [
        "licence is read-only (write access is disabled)"]),
    ("unregistered project", {"nodes": [_online()], "accounts": [_account()], "projects": ["TESTWP"], "missing_projects": ["TESTWP"]}, [
        "No specific cause was observed. Checked: parked tickets and tool waits, retry backoff, project pause, licence entitlement, node availability, version skew, toolchains, test services, account status, quota, role health, machine assignment and active leases."]),
    ("exact parked ticket display limit", {"nodes": [_online()], "waits": [{"ticket_key": f"TESTWP-{n}", "project": "TESTWP", "requirement": "gitleaks"} for n in range(1, 7)], "accepted_requirements": ["gitleaks"]}, [
        "parked tickets TESTWP-1, TESTWP-2, TESTWP-3, TESTWP-4, TESTWP-5, TESTWP-6 waiting for gitleaks; install gitleaks", "no AI account is ready (no accounts)"]),
    ("project list sorted before entitlement checks", {"nodes": [_online()], "accounts": [_account()], "projects": ["ZZ", "AA"], "project_license": {"AA": "AA denied", "ZZ": "ZZ denied"}}, [
        "licence limit stops new work for project AA: AA denied", "licence limit stops new work for project ZZ: ZZ denied"]),
    ("project license error stops later projects", {"nodes": [_online()], "accounts": [_account()], "projects": ["AA", "ZZ"], "project_license": {"AA": "AA denied", "ZZ": "ZZ denied"}, "project_license_error": True}, [
        "licence status could not be checked (RuntimeError)"]),
    ("version skew on one node with another compatible", {"nodes": [_online(), _online(name="node2")], "accounts": [_account()], "projects": ["TESTWP"], "compatibility": {"node1": (False, "behind release")}}, [
        "version skew blocks TESTWP on node1 (behind release)"]),
    ("tool gaps on one node with another clear", {"nodes": [_online(), _online(name="node2")], "accounts": [_account()], "projects": ["TESTWP"], "gaps": {"node1": ["git"]}}, [
        "No specific cause was observed. Checked: parked tickets and tool waits, retry backoff, project pause, licence entitlement, node availability, version skew, toolchains, test services, account status, quota, role health, machine assignment and active leases."]),
    ("unhealthy service on one node with another clear", {"nodes": [_online(), _online(name="node2")], "accounts": [_account()], "projects": ["TESTWP"], "project_options": {"TESTWP": {"required_services": ["postgres"]}}, "node_service_gaps": {"node1": {"postgres": "connection refused"}}}, [
        "No specific cause was observed. Checked: parked tickets and tool waits, retry backoff, project pause, licence entitlement, node availability, version skew, toolchains, test services, account status, quota, role health, machine assignment and active leases."]),
    ("account status outside ready states", {"nodes": [_online()], "accounts": [_account(status="signed_out")]}, [
        "no AI account is ready (account1=signed_out)"]),
    ("working account is ready", {"nodes": [_online()], "accounts": [_account(status="working")]}, [
        "No specific cause was observed. Checked: parked tickets and tool waits, retry backoff, project pause, licence entitlement, node availability, version skew, toolchains, test services, account status, quota, role health, machine assignment and active leases."]),
    ("probing account is ready", {"nodes": [_online()], "accounts": [_account(status="probing")]}, [
        "No specific cause was observed. Checked: parked tickets and tool waits, retry backoff, project pause, licence entitlement, node availability, version skew, toolchains, test services, account status, quota, role health, machine assignment and active leases."]),
    ("lease cooling account", {"nodes": [_online()], "accounts": [_account()], "lease_cooling": ["account1"]}, [
        "quota cooldown/backoff affects account1"]),
    ("cooldown and quota risk order", {"nodes": [_online()], "accounts": [_account(status="cooldown", quota_at_risk=True)]}, [
        "quota cooldown/backoff affects account1", "quota forecast is deferring new work for account1"]),
    ("work entitlement display limit", {"nodes": [_online()], "accounts": [_account(id="account1"), _account(id="account2"), _account(id="account3"), _account(id="account4"), _account(id="account5"), _account(id="account6"), _account(id="account7")], "account_license": {"account1": "account limit", "account2": "account limit", "account3": "account limit", "account4": "account limit", "account5": "account limit", "account6": "account limit", "account7": "account limit"}}, [
        "licence limit stops new work for 7 AI account(s) (account1, account2, account3, account4, account5, account6 and 1 more): account limit"]),
    ("account without any machine is skipped by entitlement", {"nodes": [_online()], "accounts": [_account(node="")]}, [
        "no AI account is ready (account1=idle)"]),
]


@pytest.mark.parametrize(("name", "options", "expected"), CASES, ids=[case[0] for case in CASES])
def test_idle_reasons_matches_captured_outputs(name, options, expected):
    assert _capture(options) == expected, name


def test_idle_online_nodes_keeps_only_fresh_accepting_nodes():
    ctx = _Context({"nodes": [_online(), _online(name="drained", accepting_work=False),
                               _online(name="stale", heartbeat_fresh=False)]})
    assert [node["name"] for node in incidents._idle_online_nodes(ctx)] == ["node1"]


def test_idle_parked_wait_helper_filters_by_acceptance_and_ticket(monkeypatch):
    options = {"waits": [{"ticket_key": "TESTWP-1", "project": "TESTWP", "requirement": "gitleaks"},
                          {"ticket_key": "TESTWP-2", "project": "TESTWP", "requirement": "trivy"}],
               "accepted_requirements": ["gitleaks", "trivy"]}
    _prepare(options, monkeypatch)
    assert incidents._idle_parked_tool_wait_reasons(_Context(options), {"TESTWP-1"}) == [
        "parked tickets TESTWP-1 waiting for gitleaks; install gitleaks"]


def test_idle_backoff_helper_intersects_and_sorts_ticket_keys(monkeypatch):
    options = {"backoff": {"dev": ["TESTWP-2", "TESTWP-1"], "verify": ["OTHER-1"]}}
    _prepare(options, monkeypatch)
    assert incidents._idle_retry_backoff_reasons(_Context(options), {"TESTWP-1", "TESTWP-2"}) == [
        "retry backoff for TESTWP-1, TESTWP-2"]


def test_idle_project_pause_helper_reports_only_paused_known_projects():
    repo = _Repo({"projects": ["TESTWP", "OTHER"], "project_options": {"TESTWP": {"paused": True}}})
    assert incidents._idle_project_pause_reasons(repo, {"TESTWP", "OTHER", "MISSING"}) == [
        "TESTWP is paused in project settings"]


def test_idle_project_license_helper_keeps_read_only_and_refusal_text(monkeypatch):
    options = {"projects": ["TESTWP"], "snapshot": {"read_only": True, "reason": "expired"},
               "project_license": {"TESTWP": "project limit"}}
    _prepare(options, monkeypatch)
    assert incidents._idle_project_license_reasons(_Context(options), {"TESTWP"}) == [
        "licence is read-only (expired)",
        "licence limit stops new work for project TESTWP: project limit"]


def test_idle_compatibility_helper_separates_skewed_and_compatible_nodes(monkeypatch):
    options = {"compatibility": {"node2": (False, "behind release")}}
    _prepare(options, monkeypatch)
    nodes = [_online(), _online(name="node2")]
    compatible, skewed = incidents._idle_compatible_nodes(_Context(options), nodes)
    assert compatible == [nodes[0]]
    assert skewed == ["node2 (behind release)"]


def test_idle_node_block_helper_prefers_tool_gaps_before_service_health(monkeypatch):
    options = {"gaps": {"node1": ["git"]}, "service_gaps": {"postgres": "down"}}
    _prepare(options, monkeypatch)
    project = _project({"project_options": {"TESTWP": {"required_services": ["postgres"]}}}, "TESTWP")
    assert incidents._idle_node_tool_or_service_block(_online(), project, _Repo({})) == (
        "node1 (tools missing or outside their pin: git)")


def test_idle_project_node_helper_names_the_no_serving_node_branch(monkeypatch):
    options = {"projects": ["TESTWP"], "serves": []}
    _prepare(options, monkeypatch)
    assert incidents._idle_project_node_reasons(
        _Context(options), _Repo(options), "TESTWP", [_online()]) == ["no online node serves TESTWP"]


def test_idle_node_helper_evaluates_projects_in_sorted_order(monkeypatch):
    options = {"projects": ["ZZ", "AA"], "serves": []}
    _prepare(options, monkeypatch)
    assert incidents._idle_node_reasons(
        _Context(options), _Repo(options), {"ZZ", "AA"}, [_online()]) == [
            "no online node serves AA", "no online node serves ZZ"]


def test_idle_account_inventory_counts_shared_assignment_and_quota_fields():
    options = {"accounts": [_account(node=""), _account(id="account2", status="cooldown", quota_at_risk=True)],
               "machines": [{"account_id": "account1", "node": "node1", "status": "signed_in"}]}
    inventory = incidents._idle_account_inventory(_Context(options), [_online()])
    assert inventory[2] == [options["accounts"][0]]
    assert inventory[3:] == (["account2"], ["account2"])
    assert inventory[1] == {"account1": {"node1"}}


def test_idle_quota_helper_keeps_cooldown_before_forecast():
    assert incidents._idle_quota_reasons(["account2", "account1"], ["account3"]) == [
        "quota cooldown/backoff affects account1, account2",
        "quota forecast is deferring new work for account3"]


def test_idle_account_license_helper_groups_refusals(monkeypatch):
    options = {"accounts": [_account(), _account(id="account2")],
               "account_license": {"account1": "account limit", "account2": "account limit"}}
    _prepare(options, monkeypatch)
    assert incidents._idle_account_license_reasons(
        _Context(options), options["accounts"], {}) == [
            "licence limit stops new work for 2 AI account(s) (account1, account2): account limit"]


def test_idle_account_readiness_helper_preserves_reason_sensitive_cooldown():
    accounts = [_account(status="cooldown")]
    assert incidents._idle_account_readiness_reasons(accounts, [], ["account1"], []) == [
        "AI account quota cooldown/backoff leaves no account ready (account1=cooldown)"]
    assert incidents._idle_account_readiness_reasons(
        accounts, [], ["account1"], ["quota cooldown/backoff affects account1"]) == []
