"""Characterization tests for the master action route's small steps."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control.features.runs.bodies import MasterActionBody
from aiflow.v4.control.features.runs.master_actions import (
    DEFAULT_ESCALATION_OPTIONS,
    escalation_options,
    observation_key_for,
)
from aiflow.v4.control.features.runs import routes


class _RouteCapture:
    def __init__(self):
        self.handlers = {}

    def _register(self, method, path):
        def decorator(handler):
            self.handlers[(method, path)] = handler
            return handler
        return decorator

    def get(self, path):
        return self._register("GET", path)

    def post(self, path):
        return self._register("POST", path)

    def put(self, path):
        return self._register("PUT", path)


def test_escalation_options_trim_limit_and_use_defaults():
    values = ["  first  ", " \t ", "x" * 510, "third", "fourth", "fifth", "ignored"]
    assert escalation_options(values) == ["first", "x" * 500, "third", "fourth", "fifth"]
    assert escalation_options(["", "  "]) == DEFAULT_ESCALATION_OPTIONS
    assert escalation_options(["", "  "], defaults=False) == []


def test_observation_key_uses_override_incident_ticket_and_master_fallbacks():
    body = MasterActionBody(run_id="run-1", fence=7, action="open_bug", intent="request")
    body.observation_key = "  " + "k" * 510
    assert observation_key_for(body, "") == "k" * 500

    body.observation_key = ""
    body.kind = "node_down"
    body.subject = "node-1"
    assert observation_key_for(body, "TEST-1") == "incident:node_down:node-1"

    body.kind = ""
    body.subject = ""
    updated = {"jira_updated": datetime(2025, 3, 4, 5, 6, tzinfo=timezone.utc)}
    assert observation_key_for(body, "TEST-1", updated) == "ticket:TEST-1:2025-03-04T05:06:00+00:00"
    assert observation_key_for(body, "TEST-1") == "ticket:TEST-1:current"

    body.intent = "i" * 210
    assert observation_key_for(body, "") == f"master:open_bug:{'i' * 200}"


def test_empty_intent_records_audit_then_event_then_http_error():
    calls = []
    app = _RouteCapture()
    ctx = SimpleNamespace(
        db=SimpleNamespace(one=lambda *_args: pytest.fail("empty ticket must not query tickets")),
        repo=lambda: SimpleNamespace(policy_for=lambda project: ("policy", project)),
        events=SimpleNamespace(emit=lambda event, **values: calls.append(("event", event, values))),
    )
    deps = SimpleNamespace(
        audit=lambda *values: calls.append(("audit", values)),
        console_user=lambda *_args, **_kwargs: None,
        last_dev=lambda *_args: None,
        mark_machine_auth_failure=lambda *_args: None,
        master_budget_state=lambda: None,
        master_run=lambda *_args: {"account_id": "account-1", "ticket_key": "master:global"},
        master_snapshot=lambda: None,
        node_auth=lambda: None,
        process_flow_bugs=lambda *_args: None,
        refresh_quota_forecast=lambda *_args: None,
        run_row=lambda *_args: None,
    )
    handler = routes.install(app, ctx, deps).master_action
    body = MasterActionBody(run_id="run-1", fence=7, action="retry", intent="  ")

    with pytest.raises(HTTPException) as caught:
        handler(body, node="node-1")

    reason = "master action must include the request or observation intent"
    assert caught.value.status_code == 400
    assert caught.value.detail == reason
    assert calls == [
        ("audit", ("master", "retry", "", "run-1", 7, "account-1", False, reason)),
        ("event", "master.action", {
            "ticket": "master:global", "run_id": "run-1", "fence": 7,
            "account": "account-1", "node": "node-1", "outcome": "denied",
            "reason": reason, "action": "retry",
        }),
    ]
