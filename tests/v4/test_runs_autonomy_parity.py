"""JAIF-548: the autonomy report and the plan outcome keep their results after moving out of the runs `install`.

The report is built from a fake database that answers each query by its table; the golden (tests/v4/fixtures/
runs-autonomy-parity.jsonl) holds the queries, their parameters and the result, written from the unchanged code."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.control.features.runs import autonomy, routes

GOLDEN = Path(__file__).with_name("fixtures") / "runs-autonomy-parity.jsonl"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)  # a Tuesday
D = lambda day, hour=9: datetime(2026, 10, day, hour, 0, tzinfo=timezone.utc)  # noqa: E731


def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_iso(item) for item in value]
    return value


class FakeDb:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def all(self, sql, params=()):
        self.calls.append([re.sub(r"\s+", " ", sql)[:70], _iso(params)])
        for marker, key in (("FROM tickets t WHERE t.status='Done'", "finished"), ("FROM master_escalations", "needed"),
                            ("FROM events WHERE type IN", "repeated"), ("FROM flow_bug_tracker", "tracker"),
                            ("FROM accounts ORDER BY id", "accounts")):
            if marker in sql:
                return list(self.rows.get(key, []))
        raise AssertionError(sql)


SCENARIOS = {
    "empty": dict(),
    "this week and older": dict(
        finished=[{"key": "T-1", "summary": "a", "done_at": D(26)}, {"key": "T-2", "summary": "b", "done_at": D(20)},
                  {"key": "T-3", "summary": "c", "done_at": datetime(2026, 11, 2, 8, tzinfo=timezone.utc)}],
        needed=[{"observation_key": "k1", "ticket": "T-1", "reason": "r", "options": ["x"], "sent_at": D(27)},
                {"observation_key": "k2", "ticket": "T-9", "reason": "r", "options": [],
                 "sent_at": datetime(2026, 11, 2, 9, tzinfo=timezone.utc)}],
        repeated=[{"ts": D(28), "type": "flow_bug.created"}, {"ts": D(1), "type": "flow_bug.repeated"}],
        tracker=[{"ticket_key": "T-5", "signature": "sig", "repeat_count": 3, "updated_at": D(30), "summary": "s",
                  "status": "Done"}],
        accounts=[{"id": "codex-1", "provider": "codex", "quota": {}, "quota_source": "", "quota_observed_at": None}]),
    "no accounts": dict(finished=[{"key": "T-1", "summary": "a", "done_at": D(21)}]),
}


def _namespace(rows, monkeypatch):
    ctx = SimpleNamespace(db=FakeDb(rows))
    monkeypatch.setattr(autonomy, "utcnow", lambda: NOW)
    monkeypatch.setattr(autonomy.escalations, "weekly_metrics", lambda db, weeks: {"weeks": weeks})
    monkeypatch.setattr(autonomy.quota_reporting, "assessments",
                        lambda db, accounts, now: {a["id"]: {"state": "ok", "now": now.isoformat()} for a in accounts})
    deps = SimpleNamespace(**{name: (lambda *a, **k: None) for name in (
        "audit", "console_user", "last_dev", "mark_machine_auth_failure", "master_budget_state", "master_run",
        "master_snapshot", "node_auth", "process_flow_bugs", "refresh_quota_forecast", "run_row")})
    app = SimpleNamespace(**{m: (lambda *a, **k: (lambda h: h)) for m in ("get", "post", "put", "patch", "delete")})
    return ctx, routes.install(app, ctx, deps)


def _observe(name, weeks, scope, monkeypatch):
    ctx, namespace = _namespace(SCENARIOS[name], monkeypatch)
    result = namespace.autonomy_report(weeks, scope=scope)
    return {"name": name, "weeks": weeks, "scope": scope, "calls": ctx.db.calls,
            "result": json.loads(json.dumps(result, default=_iso))}


CASES = [(name, weeks, scope) for name in SCENARIOS for weeks in (1, 4, 8) for scope in (None, ("AAA", "BBB"))]


def _golden():
    return {(r["name"], r["weeks"], tuple(r["scope"]) if r["scope"] else None): r
            for r in (json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines())}


@pytest.mark.parametrize("name, weeks, scope", CASES)
def test_autonomy_report_matches_the_recorded_result(name, weeks, scope, monkeypatch):
    expected = _golden()[(name, weeks, scope)]
    observed = _observe(name, weeks, list(scope) if scope else None, monkeypatch)
    observed = json.loads(json.dumps(observed))
    expected = json.loads(json.dumps(expected))
    assert observed == expected


def test_there_are_at_least_twenty_recorded_cases():
    assert len(CASES) >= 18 and len(_golden()) == len(CASES)
