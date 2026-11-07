"""JAIF-349: a master cycle calls the model only when a rule finds an item to decide, and says so when it does not."""
from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace

import pytest

from aiflow.v4.worker import master as worker_master
from aiflow.v4.worker.dev import Claimed

MASTER_SCOPE = "master:global"

# Items that the cycle used to send to the model, but that no longer start one on their own.
QUIET = {"open_incidents": [], "needs_owner": [{"key": "T-1"}], "running_clocks": [{"passed": ["warn"]}],
         "offline_nodes": [{"name": "n1"}], "quota_accounts": [{"account": "a"}],
         "quota_incidents": [{"kind": "account_quota"}], "flow_bug_reports": [], "review_requested": False,
         "autonomy": {"remaining_seconds": 600}}

TRIGGERS = [
    ("open-incident", {"open_incidents": [{"kind": "ticket_stuck", "subject": "T-2"}]}),
    ("hard-limit", {"running_clocks": [{"passed": ["warn", "hard"]}]}),
    ("repeated-failure", {"flow_bug_reports": [{"signature": "sig"}]}),
    ("dashboard-request", {"review_requested": True}),
]


class FakeApi:
    def __init__(self, snapshot, reports=()):
        self.snapshot, self.reports = snapshot, list(reports)

    def call(self, method, path, body=None, params=None):
        if (method, path) == ("GET", "/v1/master/context"):
            return dict(self.snapshot)
        assert (method, path) == ("POST", "/v1/master/flow-bugs")
        return {"reports": self.reports}


class CountingAdapter:
    def __init__(self, calls):
        self.calls = calls

    def argv(self, _spec):
        return []


def quota_response():
    """The model answered with a quota error, so the cycle ends after one model call with no action."""
    return SimpleNamespace(error_kind="quota", error_message="later", tokens_in=1, tokens_out=1, cost_usd=0.0,
                           quota={}, quota_observed_at=None, reset_at=None, final_text="", log_path="")


def claimed_master():
    return Claimed(run_id=str(uuid.uuid4()), fence=3, run_token="test-token", account="claude-01", provider="claude",
                   model="opus", effort="high", ticket={"key": MASTER_SCOPE}, project={"node": "cc"},
                   identity=("bot", "bot@example.test"), step="master")


def run_cycle(monkeypatch, tmp_path, api):
    """Run one cycle and return its result and the number of model calls it made."""
    model_calls = []
    monkeypatch.setattr(worker_master, "get_adapter", lambda _provider: CountingAdapter(model_calls))
    monkeypatch.setattr(worker_master, "run_with_inbox", lambda *_args, **_kwargs: model_calls.append(1)
                        or quota_response())
    heartbeat = SimpleNamespace(lost=threading.Event(), progress={})
    result = {"fence": 3, "outcome": "failed", "details": {}}
    result = worker_master.run_master_cycle(api, claimed_master(), heartbeat, result, tmp_path, tmp_path,
                                            str(tmp_path), "http://control.test", [], 0.0)
    return result, len(model_calls)


def test_items_that_no_longer_trigger_a_review_give_no_trigger():
    assert worker_master.review_triggers(QUIET) == []


@pytest.mark.parametrize("trigger, overrides", TRIGGERS, ids=[name for name, _ in TRIGGERS])
def test_each_trigger_names_itself(trigger, overrides):
    assert worker_master.review_triggers({**QUIET, **overrides}) == [trigger]


def test_several_triggers_are_all_named_in_rule_order():
    snapshot = {**QUIET, "open_incidents": [{"kind": "run_slow"}], "review_requested": True}
    assert worker_master.review_triggers(snapshot) == ["open-incident", "dashboard-request"]


def test_quiet_cycle_makes_no_model_call_and_says_no_action_is_needed(tmp_path, monkeypatch):
    result, model_calls = run_cycle(monkeypatch, tmp_path, FakeApi(QUIET))

    assert model_calls == 0
    assert result["outcome"] == "done"
    assert result["summary"] == "No action needed this cycle."
    assert result["details"]["no_watch"] is True and result["details"]["model_time_s"] == 0


@pytest.mark.parametrize("trigger, overrides", TRIGGERS, ids=[name for name, _ in TRIGGERS])
def test_each_trigger_starts_one_model_call(trigger, overrides, tmp_path, monkeypatch):
    snapshot = {**QUIET, **overrides}
    reports = snapshot.get("flow_bug_reports", [])
    result, model_calls = run_cycle(monkeypatch, tmp_path, FakeApi(snapshot, reports))

    assert model_calls == 1
    assert result["outcome"] == "quota"
