"""Characterize shared account routes installed by ``install`` before the installer split."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from aiflow.v4.control import shared_accounts


FIXTURE = Path(__file__).with_name("fixtures") / "shared_accounts_install_parity.jsonl"


class _Result:
    def __init__(self, value=None):
        self.value = value

    def fetchone(self):
        return self.value


class _DB:
    def __init__(self, scenario, calls):
        self.scenario, self.calls = scenario, calls
        self.machine_reads = 0
        self.pool = SimpleNamespace(connection=lambda: _Connection(self))

    def _label(self, sql):
        for fragment, name in (("SELECT name FROM nodes", "node_names"), ("shared_accounts WHERE managed", "managed_accounts"),
                               ("managed_ai_accounts", "managed_ai"), ("SELECT managed FROM shared_accounts", "managed_row"),
                               ("account_machines", "account_machine"), ("nodes WHERE name", "node_exists"),
                               ("shared_accounts", "shared_account"), ("accounts", "account")):
            if fragment in sql:
                return name
        return "other"

    def all(self, sql, params=()):
        label = self._label(sql)
        self.calls.append(["all", label])
        if label == "node_names":
            return [{"name": name} for name in self.scenario.get("nodes", ["node-a"])]
        if label == "managed_accounts":
            return deepcopy(self.scenario.get("managed_accounts", []))
        return []

    def one(self, sql, params=()):
        label = self._label(sql)
        self.calls.append(["one", label])
        if label == "node_exists":
            return {"x": 1} if self.scenario.get("node_exists", True) else None
        if label == "managed_ai":
            return {"id": "alpha-1"} if self.scenario.get("managed_ai") else None
        if label == "managed_row":
            return {"managed": self.scenario.get("managed", True)} if self.scenario.get("row_exists", True) else None
        if label == "account_machine":
            self.machine_reads += 1
            row = deepcopy(self.scenario.get("machine_row", {"status": "pending", "signin_attempt": 1}))
            if self.machine_reads > 1 and row:
                if self.scenario.get("current_missing"):
                    return None
                row["status"] = self.scenario.get("current_status", row["status"])
                row["signin_attempt"] = self.scenario.get("current_attempt", row["signin_attempt"])
            return row
        return None

    def run(self, sql, params=()):
        label = self._label(sql)
        self.calls.append(["run", label])
        if "signin_attempt=signin_attempt + 1" in sql:
            return self.scenario.get("retry_changed", 1)
        if "signin_link=%s" in sql:
            return self.scenario.get("prompt_changed", 1)
        if "status='signed_in'" in sql:
            return self.scenario.get("complete_changed", 1)
        return 1


class _Connection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=()):
        label = "active_runs" if "count(*) AS n FROM runs" in sql else self.db._label(sql)
        self.db.calls.append(["execute", label])
        if label == "active_runs":
            return _Result({"n": self.db.scenario.get("active", 0)})
        if label == "managed_row":
            value = {"managed": self.db.scenario.get(
                "locked_managed", self.db.scenario.get("managed", True))}
            return _Result(value if self.db.scenario.get("row_exists", True) else None)
        return _Result()


class _Account:
    def __init__(self, spec):
        self.id = spec.get("id", "alpha-1")
        self.provider = spec.get("provider", "claude")
        self.tier, self.roles = "middle", ["dev"]
        self.explicit_only, self.sessions = False, spec.get("sessions", 2)
        self.machines = list(spec.get("machines", ["node-a"]))

    def runs_on(self, node):
        return node in self.machines


class _Repo:
    def __init__(self, spec):
        self.accounts = [_Account(row) for row in spec.get("accounts", [])]
        self.items = {account.id: account for account in self.accounts}
        self.items.setdefault("alpha-1", _Account(spec.get("account", {})))
        if spec.get("unknown_account"):
            self.items.pop(spec["unknown_account"], None)

    def account(self, account_id):
        return self.items.get(account_id)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5, tzinfo=tz or timezone.utc)


def _invoke_account_route(endpoints, name, request, module):
    if name == "list_accounts":
        return endpoints[name](None)
    if name == "create_account":
        return endpoints[name](module.AccountBody(**request["body"]), None)
    if name == "set_limit":
        return endpoints[name](request.get("account_id", "alpha-1"),
                               module.LimitBody(**request["body"]), None)
    return endpoints[name](request.get("account_id", "alpha-1"),
                           module.MachineBody(**request["body"]), None)


def _invoke_machine_route(endpoints, name, request):
    args = (request.get("account_id", "alpha-1"), request.get("node", "node-a"), None)
    if name == "remove_machine":
        return endpoints[name](*args, confirm=request.get("confirm", False))
    return endpoints[name](*args)


def _invoke_signin_route(endpoints, name, request, module):
    account_id, node = request.get("account_id", "alpha-1"), request.get("node", "node-a")
    if name == "signin_prompt":
        return endpoints[name](account_id, module.SigninPromptBody(**request["body"]), node)
    return endpoints[name](account_id, module.SigninCompleteBody(**request["body"]), node)


def _invoke_endpoint(endpoints, name, request, module):
    if name == "__routes__":
        return [[route.path, sorted(route.methods), getattr(route, "status_code", None)]
                for route in endpoints["__app__"].routes if hasattr(route, "methods")]
    if name in {"list_accounts", "create_account", "set_limit", "enable_machine"}:
        return _invoke_account_route(endpoints, name, request, module)
    if name in {"retry_signin", "remove_machine", "remove_account"}:
        if name == "remove_account":
            return endpoints[name](request.get("account_id", "alpha-1"), None, request.get("confirm", False))
        return _invoke_machine_route(endpoints, name, request)
    return _invoke_signin_route(endpoints, name, request, module)


def _run_case(case, monkeypatch, module=shared_accounts):
    calls = []
    scenario = case.get("scenario", {})
    ctx = SimpleNamespace(db=_DB(scenario, calls), repo=lambda: _Repo(scenario),
                          events=SimpleNamespace(emit=lambda *args, **kwargs: calls.append(["emit", *args])))
    user = SimpleNamespace(subject="person@example.test", permissions=set(case.get("permissions", ["*"])))
    monkeypatch.setattr(module, "online_nodes", lambda _db: {"node-a"})
    monkeypatch.setattr(module, "running_by_node", lambda _db, _account: {"node-a": 1})
    monkeypatch.setattr(module, "machines_view",
                        lambda _db, account, _status, *, include_signin=False:
                        [{"node": node, "include_signin": include_signin} for node in account.machines])
    monkeypatch.setattr(module, "publish_for",
                        lambda _ctx, event: calls.append(["publish", type(event).__name__, vars(event)]))
    monkeypatch.setattr(module, "datetime", _FrozenDateTime)
    app = FastAPI()
    app.state.sync_accounts = lambda: calls.append(["sync_accounts"]) or []

    def console_user(_request, permissions, **_kwargs):
        calls.append(["console_user", sorted(permissions)])
        if case.get("deny"):
            raise HTTPException(403, "permission denied")
        return user

    module.install(app, ctx, console_user, lambda *_args, **_kwargs: None, lambda *_args, **_kwargs: "node-a")
    endpoints = {route.endpoint.__name__: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
    endpoints["__app__"] = app
    req = case.get("request", {})
    try:
        result = _invoke_endpoint(endpoints, case["handler"], req, module)
        outcome = {"result": result, "error": None}
    except HTTPException as error:
        outcome = {"result": None, "error": {"status": error.status_code, "detail": error.detail}}
    return {"outcome": outcome, "calls": calls}


def _cases():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_installed_shared_account_routes_match_recorded_behavior(case, monkeypatch):
    assert _run_case(case, monkeypatch) == case["expected"]


def test_parity_table_covers_twenty_route_scenarios():
    assert len(_cases()) >= 20
