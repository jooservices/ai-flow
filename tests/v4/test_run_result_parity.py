"""Characterize the current run-result route through its installed handler."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control import api
from aiflow.v4.control.features.runs import routes
from aiflow.v4.control.features.runs.bodies import ResultBody


FIXTURE = Path(__file__).parent / "fixtures" / "run_result_parity.jsonl"
STARTED = datetime(2024, 1, 1, tzinfo=timezone.utc)
GOLDEN_CASES = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]


class _App:
    def __init__(self):
        self.handlers = {}

    def route(self, path):
        return lambda handler: self.handlers.setdefault(path, handler) and handler

    post = get = put = route


def _normal(value):
    if hasattr(value, "obj"):
        return _normal(value.obj)
    if hasattr(value, "model_dump"):
        return _normal(value.model_dump(mode="json"))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _normal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    return value


class _Fakes:
    def __init__(self, fake, calls):
        self.fake, self.calls = fake, calls
        self.r = object()

    def one(self, _query, _params):
        self.calls.append(["db.one", "quota"])
        return {"quota": {"observed": 1}}

    def run(self, query, params):
        label = "cooldown" if "cooldown_until" in query else "account-status" if "UPDATE accounts" in query else "run-details"
        self.calls.append(["db.run", label, _normal(params)])
        return self.fake.get("account_changed", True)

    def valid(self, ticket, run_id, fence):
        self.calls.append(["lease.valid", ticket, run_id, fence])
        return self.fake.get("owner", True)

    def cooldown(self, account, seconds, reason):
        self.calls.append(["lease.cooldown", account, seconds, reason])

    def add_worklog(self, ticket, seconds, started, text):
        self.calls.append(["jira.worklog", ticket, seconds, started.isoformat(), text])
        if self.fake.get("worklog_error"):
            raise RuntimeError(self.fake["worklog_error"])

    def warning(self, message, *args):
        self.calls.append(["warning", message % args])


class _Record:
    def __init__(self, calls, name, result=None, error=None):
        self.calls, self.name, self.result, self.error = calls, name, result, error

    def __call__(self, *args, **kwargs):
        self.calls.append([self.name, _normal(args[1:]), _normal(kwargs)])
        if self.error:
            raise RuntimeError(self.error)
        return self.result


class _Harness:
    def __init__(self, fake, monkeypatch):
        self.fake, self.calls = fake, []
        self.run = {"node": "node-a", "fence": 4, "ticket_key": "TEST-1", "account_id": "acct-a",
                    "provider": "claude", "model": "opus", "started_at": STARTED, "step": "dev", "project": "TEST"}
        self.run.update(fake.get("run", {}))
        self.now = datetime.fromisoformat(fake.get("now", "2024-01-01T00:10:00+00:00"))
        values = {"fence": 4, "outcome": "done", "summary": "completed", "details": {}}
        values.update(fake.get("body", {}))
        self.body = ResultBody(**values)
        fakes = _Fakes(fake, self.calls)
        self.ctx = SimpleNamespace(db=fakes, leases=fakes,
                                   settings=SimpleNamespace(jira_site="https://jira.example.test"),
                                   jira=fakes if fake.get("jira", True) else None)
        self.logger = fakes
        self._patch_routes(monkeypatch)

    def _record(self, name, result=None, error=None):
        return _Record(self.calls, name, result, error)

    def _criteria(self, criteria):
        self.calls.append(["post-merge-criteria", _normal(criteria)])
        if self.fake.get("criteria_error"):
            raise RuntimeError(self.fake["criteria_error"])
        return [*criteria, "recorded"]

    def _finish(self, _ctx, _run, _run_id, result_body, details, node):
        self.calls.append(["finish", result_body.outcome, result_body.error_kind, _normal(details), node])
        return self.fake.get("late", False)

    def _policy(self, _ctx, node, result_body, details, timestamp):
        self.calls.append(["policy", node, result_body.outcome, result_body.error_kind, _normal(details), timestamp])
        policy = self.fake.get("policy")
        if policy:
            return policy["outcome"], policy["error_kind"], policy["details"]
        return result_body.outcome, result_body.error_kind, details

    def _patch_routes(self, monkeypatch):
        self._patch_policy_and_runs(monkeypatch)
        self._patch_result_recorders(monkeypatch)
        monkeypatch.setattr(routes, "run_finished", lambda _ctx, **kw: self.calls.append(["run-finished", _normal(kw)]))
        monkeypatch.setattr(api, "log", self.logger)

    def _patch_policy_and_runs(self, monkeypatch):
        monkeypatch.setattr(routes, "utcnow", lambda: self.now)
        monkeypatch.setattr(routes, "load_policy", SimpleNamespace(result_after_load_failure=self._policy))
        monkeypatch.setattr(routes, "environment_reports", SimpleNamespace(
            UnmappedPostMergeCriteria=RuntimeError, record_post_merge_criteria=lambda *_args: self._criteria(_args[-1]),
            record_missing=self._record("environment-missing", self.fake.get("recorded_environment", []))))
        monkeypatch.setattr(routes, "run_inbox_control", SimpleNamespace(
            finish=self._finish, account_update_allowed=self._account_update_allowed))

    def _patch_result_recorders(self, monkeypatch):
        monkeypatch.setattr(routes, "support_bundle", SimpleNamespace(
            store_run_log=self._record("support-log", error=self.fake.get("support_error"))))
        monkeypatch.setattr(routes, "quota_refresh", SimpleNamespace(record_run_result=self._record("quota-result")))
        monkeypatch.setattr(routes, "tooling", SimpleNamespace(
            record_missing=self._record("tool-missing", self.fake.get("recorded_tools", []))))
        monkeypatch.setattr(routes, "knowledge", SimpleNamespace(
            capture_run=self._record("knowledge-capture", error=self.fake.get("capture_error"))))
        monkeypatch.setattr(routes, "role_health", SimpleNamespace(
            evaluate=self._record("role-evaluate", error=self.fake.get("role_error"))))

    def _account_update_allowed(self, *_args):
        self.calls.append(["account-update-allowed"])
        return self.fake.get("status_allowed", True)

    def _run_row(self, run_id):
        self.calls.append(["run-row", run_id])
        return self.run

    def _mark_auth(self, account, node, summary):
        self.calls.append(["auth-failure", account, node, summary])

    def _refresh_quota(self, account, provider, quota):
        self.calls.append(["quota-forecast", account, provider, _normal(quota)])

    def _last_dev(self, ticket):
        self.calls.append(["last-dev", ticket])
        return self.fake.get("last_dev")

    def invoke(self):
        deps = SimpleNamespace(**{name: object() for name in (
            "audit", "console_user", "master_budget_state", "master_run", "master_snapshot", "node_auth",
            "process_flow_bugs")}, last_dev=self._last_dev, mark_machine_auth_failure=self._mark_auth,
            refresh_quota_forecast=self._refresh_quota, run_row=self._run_row)
        app = _App()
        routes.install(app, self.ctx, deps)
        handler = app.handlers["/v1/runs/{run_id}/result"]
        try:
            response = handler("run-1", self.body, self.fake.get("node", "node-a"))
            result = {"response": _normal(response)}
        except HTTPException as exc:
            result = {"http": {"status": exc.status_code, "detail": str(exc.detail)}}
        except Exception as exc:  # unexpected errors are pinned too if the old route raises one
            result = {"error": {"type": type(exc).__name__, "text": str(exc)}}
        return {**result, "calls": self.calls}


def characterize(spec: dict, monkeypatch: pytest.MonkeyPatch) -> dict:
    return _Harness(spec, monkeypatch).invoke()


@pytest.mark.parametrize("item", GOLDEN_CASES, ids=lambda item: item["id"])
def test_run_result_matches_characterized_case(item, monkeypatch):
    assert len(GOLDEN_CASES) == 26
    assert characterize(item["input"], monkeypatch) == item["expect"]


def test_prepare_run_result_removes_control_owned_and_raw_log_fields():
    body = ResultBody(fence=4, outcome="done", details={"pr_url_source": "worker", "quota_reservation": 1,
                                                         "support_log_text": "log", "support_log_truncated": True,
                                                         "kept": 1})
    details, run_log, outcome, error = routes._prepare_run_result(None, {"step": "dev"}, body, "node-a", False)
    assert (details, run_log, outcome, error) == ({"kept": 1}, "log", "done", "")


def test_finish_run_result_preserves_update_decisions(monkeypatch):
    monkeypatch.setattr(routes, "run_inbox_control", SimpleNamespace(finish=lambda *_: False,
                                                                       account_update_allowed=lambda *_: True))
    assert routes._finish_run_result(None, {"account_id": "acct-a"}, "run-1", None, {}, "node-a", True) == (
        False, True, True)


def test_update_run_result_account_records_quota_without_database_services(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "quota_refresh", SimpleNamespace(record_run_result=lambda *a, **k: calls.append((a, k))))
    routes._update_run_result_account(SimpleNamespace(db=object()), {"account_id": "acct-a"}, "run-1", "node-a",
                                      SimpleNamespace(quota={"requests": 1}, error_kind=""), None, False, True, False,
                                      lambda *_: None, lambda *_: None, None)
    assert calls and calls[0][1] == {"update_account": True}


def test_record_run_result_missing_marks_unaccepted_tool_as_failed(monkeypatch):
    writes = []
    monkeypatch.setattr(routes, "tooling", SimpleNamespace(record_missing=lambda *_: []))
    ctx = SimpleNamespace(db=SimpleNamespace(run=lambda *args: writes.append(args)))
    body = SimpleNamespace(outcome="tool_missing", details={"missing_tools": ["compiler"]}, summary="missing")
    outcome = routes._record_run_result_missing(ctx, {"step": "dev"}, "run-1", body, {}, False, "tool_missing")
    assert outcome == "failed" and len(writes) == 1


def test_capture_run_result_followups_records_capture_and_duration(monkeypatch):
    now, captures = STARTED, []
    monkeypatch.setattr(routes, "utcnow", lambda: now)
    monkeypatch.setattr(routes, "knowledge", SimpleNamespace(capture_run=lambda *args: captures.append(args)))
    ctx = SimpleNamespace(db=object(), settings=SimpleNamespace(jira_site="https://jira.example.test"), jira=None)
    dur = routes._capture_run_result_followups(ctx, {"started_at": STARTED}, "run-1",
                                               SimpleNamespace(outcome="done", fence=4), False, lambda *_: None, None)
    assert dur == 0 and len(captures) == 1


def test_evaluate_run_result_health_checks_the_other_verify_account(monkeypatch):
    checked = []
    monkeypatch.setattr(routes, "role_health", SimpleNamespace(evaluate=lambda _ctx, account: checked.append(account)))
    run = {"step": "verify", "account_id": "acct-a", "ticket_key": "TEST-1"}
    routes._evaluate_run_result_health(None, run, "run-1", False, lambda _: {"account_id": "acct-b"}, None)
    assert checked == ["acct-a", "acct-b"]
