"""Characterize the node heartbeat route's response and ordered effects."""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from aiflow.v4.control import sa, system_check_api
from aiflow.v4.control.features.fleet import routes


FIXTURE = Path(__file__).parent / "fixtures" / "node_heartbeat_parity.jsonl"


def _plain(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, SimpleNamespace):
        return "SimpleNamespace"
    if isinstance(value, _Repo):
        return "_Repo"
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class _App:
    def __init__(self):
        self.routes = {}

    def post(self, path, **_kwargs):
        return self._register(path)

    def get(self, path, **_kwargs):
        return self._register(path)

    def put(self, path, **_kwargs):
        return self._register(path)

    def patch(self, path, **_kwargs):
        return self._register(path)

    def delete(self, path, **_kwargs):
        return self._register(path)

    def _register(self, path):
        def register(function):
            self.routes[path] = function
            return function
        return register


class _Account:
    def __init__(self, row):
        self.id = row["id"]
        self.paused = row.get("paused", False)
        self.sessions = row.get("sessions", 1)
        self.provider = row.get("provider", "codex")
        self.nodes = row.get("nodes", ["node-a"])

    def runs_on(self, node):
        return node in self.nodes


class _Repo:
    def __init__(self, config):
        self.accounts = {row["id"]: _Account(row) for row in config.get("accounts", [])}
        self.assigned = [self.accounts[row["id"]] for row in config.get("assigned", [])]
        self.projects = {row["key"]: SimpleNamespace(
            stack=row.get("stack", ""), stacks=row.get("stacks", []),
            repositories=row.get("repositories", []),
            environment_items=[SimpleNamespace(name=item, secret=True) for item in row.get("secrets", [])],
            required_services=row.get("required_services", [])) for row in config.get("projects", [])}
        self.versions = config.get("versions", {})
        self.config = config

    def account(self, account_id):
        return self.accounts.get(account_id)

    def accounts_on(self, _node):
        return self.assigned

    def projects_for_node(self, _node, _projects, _dynamic):
        return self.config.get("node_projects", [])

    def tools_for_node(self, _node, _projects):
        return self.config.get("tools", [])


class _DB:
    def __init__(self, config, trace):
        self.config = config
        self.trace = trace

    def one(self, sql, params=()):
        label = "previous" if "accepting_work,release_version" in sql else "node_state"
        self.trace.append(["db.one", label, _plain(params)])
        if label == "previous":
            return self.config.get("previous")
        return self.config.get("node_state", {"projects": [], "dynamic": False})

    def all(self, sql, params=()):
        if "SELECT id,provider" in sql:
            label, result = "sign_ins", self.config.get("sign_ins", [])
        elif "SELECT account_id FROM account_machines" in sql:
            label, result = "pending_accounts", self.config.get("pending_accounts", [])
        else:
            label, result = "pending_signins", self.config.get("pending_signins", [])
        self.trace.append(["db.all", label, _plain(params)])
        return result

    def run(self, sql, params=()):
        labels = (("UPDATE nodes SET", "node_update"), ("UPDATE account_machines SET credential", "credential_machine"),
                  ("UPDATE accounts SET credential", "credential_account"), ("UPDATE accounts SET worker_state", "worker_account"),
                  ("UPDATE account_machines SET worker_state", "worker_machine"), ("UPDATE accounts SET idle_since", "worker_timestamps"))
        label = next((name for prefix, name in labels if sql.startswith(prefix)), "other")
        self.trace.append(["db.run", label, _plain(params)])


def execute_case(case):
    config = case["input"]
    trace = []
    repo = _Repo(config)
    ctx = SimpleNamespace(db=_DB(config, trace), repo=lambda: repo,
                          events=SimpleNamespace(
                              ship=lambda *args, **kwargs: trace.append(["event.ship", _plain(args), _plain(kwargs)]),
                              emit=lambda *args, **kwargs: trace.append(["event.emit", _plain(args), _plain(kwargs)])))
    body = system_check_api.HeartbeatBody(**config.get("body", {}))

    def record(label, result=None):
        def call(*args, **kwargs):
            trace.append([label, _plain(args), _plain(kwargs)])
            return result
        return call

    def reported(request, previous):
        trace.append(["reported_heartbeat", _plain(previous)])
        prior = previous or {}
        metrics = request.metrics if "metrics" in request.model_fields_set else prior.get("metrics") or {}
        return (request.tools if "tools" in request.model_fields_set else prior.get("tools") or {},
                request.release_version if "release_version" in request.model_fields_set else prior.get("release_version") or "",
                metrics, request.services if "services" in request.model_fields_set else metrics.get("services", {}))

    def sanitize(settings):
        trace.append(["sanitize_inventory", _plain(settings)])
        return settings

    def fleet_release(node, version):
        result = config.get("fleet_release_result", "target:" + version)
        trace.append(["fleet_release", node, version, result])
        return result

    replacements = {
        "reported_heartbeat": reported,
        "refresh_heartbeat": record("refresh_heartbeat"),
        "sanitize_inventory": sanitize,
        "record_heartbeat": record("record_heartbeat"),
        "confirm_node_environment": record("confirm_node_environment"),
        "resolve_available": record("resolve_available"),
        "install_requests": record("install_requests", config.get("tool_repairs", [])),
        "pending_probes": record("pending_probes", config.get("probes", [])),
        "update_slot_pressure": record("update_slot_pressure"),
        "close_incident": record("close_incident"),
    }
    response = None
    error = None
    deps = SimpleNamespace(audit=None, audit_in=None, console_user=None, create_join_grant=None,
                           current_release=None, fleet_release=fleet_release, node_auth=lambda: "node-a",
                           release_assets=None, require_capacity=None)
    app = _App()
    routes.install(app, ctx, deps)
    handler = app.routes["/v1/nodes/{name}/heartbeat"]
    with patch.object(routes, "J", lambda value: value), \
            patch.object(routes.system_check_api, "reported_heartbeat", reported), \
            patch.object(routes.system_check_api, "refresh_heartbeat", replacements["refresh_heartbeat"]), \
            patch.object(routes.runtime_settings, "sanitize_inventory", sanitize), \
            patch.object(routes.load_policy, "record_heartbeat", replacements["record_heartbeat"]), \
            patch.object(routes.environment_reports, "confirm_node_environment", replacements["confirm_node_environment"]), \
            patch.object(routes.tooling, "resolve_available", replacements["resolve_available"]), \
            patch.object(routes.tooling, "install_requests", replacements["install_requests"]), \
            patch.object(routes.tooling, "pending_probes", replacements["pending_probes"]), \
            patch.object(sa, "update_slot_pressure", replacements["update_slot_pressure"]), \
            patch("aiflow.v4.control.incidents.close_incident", replacements["close_incident"]):
        try:
            response = handler(config.get("route_name", "node-a"), body, config.get("node", "node-a"))
        except HTTPException as exc:
            error = {"status": exc.status_code, "detail": exc.detail}
    if error:
        return {"error": error, "trace": trace}
    return {"response": _plain(response), "trace": trace}


@pytest.mark.parametrize("case", [json.loads(row) for row in FIXTURE.read_text().splitlines()],
                         ids=lambda case: case["name"])
def test_node_heartbeat_matches_unchanged_characterization(case):
    assert execute_case(case) == case["expected"]


def test_node_heartbeat_characterization_has_twenty_cases():
    assert len(FIXTURE.read_text().splitlines()) >= 20


def test_merge_worker_reports_keeps_highest_state_and_counts_each_instance():
    assert routes.heartbeat.merge_worker_reports([
        {"account": "a1", "state": "idle", "pid": 1},
        {"account": "a1", "state": "working", "pid": 2},
        {"account": "a1", "state": "working", "pid": 3},
        {"account": "a2", "alive": False},
        {"state": "working"},
    ]) == {
        "a1": {"account": "a1", "state": "working", "pid": 2, "_state": "working", "instances": 3},
        "a2": {"account": "a2", "alive": False, "_state": "stopped", "instances": 1},
    }
