"""Characterize the current human review queue route before its extraction."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from aiflow.v4.control import review_metrics
from aiflow.v4.control.review_queue_api import (
    HumanReviewThreads,
    ReviewQueueAPI,
    _build_recent_human_review_threads,
    _human_review_actions,
    _human_review_preview_url,
    _human_review_thread_filters,
)


STAMP = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)
EXPECTED_PATH = Path(__file__).parent / "fixtures" / "human-reviews-parity.jsonl"
QUERY_TAGS = (
    ("SELECT key, status, blocked_by FROM tickets", "queue_candidates"),
    ("SELECT key,status FROM tickets WHERE key=ANY(%s)", "blocker_statuses"),
    ("SELECT key, project, status, labels, blocked_by", "metrics_tickets"),
    ("SELECT ts,type,ticket,project,outcome,reason,data FROM events", "metrics_events"),
    ("SELECT ticket_key,project,step,account_id,outcome", "metrics_runs"),
    ("SELECT ts,ticket,action,target,accepted,result,payload FROM write_audit", "metrics_audits"),
    ("SELECT key, project, type, summary, description", "tickets"),
    ("WHERE key=%s", "epics"),
    ("SELECT DISTINCT ON (ticket_key) ticket_key,pr_url", "dev_runs"),
    ("SELECT DISTINCT ON (ticket_key) ticket_key,step,outcome", "latest_runs"),
    ("SELECT DISTINCT ON (ticket_key) ticket_key,outcome", "verify_runs"),
    ("SELECT DISTINCT ON (ticket) ticket,ts", "asked_by_key"),
    ("SELECT DISTINCT ON (ticket_key) ticket_key,started_at,status", "triage_by_key"),
    ("SELECT ticket,max(ts) AS latest FROM events", "thread_event_dates"),
    ("SELECT ticket_key AS ticket,max(started_at) AS latest FROM runs", "thread_run_dates"),
    ("FROM events WHERE ticket=ANY(%s) AND type='review.action'", "thread_events"),
    ("PARTITION BY ticket_key,step ORDER BY started_at DESC", "thread_runs"),
    ("SELECT key,project,summary,status FROM tickets", "missing_thread_tickets"),
)


class Project:
    def __init__(self, name: str, environments: dict | None = None, trace: list | None = None):
        self.name = name
        self.environments = environments or {}
        self.trace = trace

    def repo_for(self, ticket: dict) -> str:
        if self.trace is not None:
            self.trace.append(["repo_for", self.name.lower(), bool(ticket.get("epic"))])
        return f"org/{self.name.lower()}"


class Database:
    def __init__(self, plan: dict, trace: list):
        self.plan = plan
        self.calls: list[str] = []
        self.trace = trace

    def all(self, query: str, params=()):
        tag = next((name for marker, name in QUERY_TAGS if marker in query), None)
        if tag is None:
            raise AssertionError(f"Uncharacterized human review query: {query}")
        self.calls.append(tag)
        call_params = ["<datetime>" if isinstance(param, datetime) else param for param in params]
        self.trace.append(["db", tag, call_params])
        return self.plan.get(tag, [])

    def one(self, query: str, params=()):
        raise AssertionError(f"Unexpected one-row query: {query}")


class Context:
    def __init__(self, plan: dict, scope: tuple[str, ...] | None, console_login: bool, trace: list):
        self.trace = trace
        self.db = Database(plan, trace)
        projects = {
            "DEMO": Project("Demo", {"preview": {"url": "https://preview.example.test"}}, trace),
            "TESTFLOW": Project("Testflow", {"preview": "https://preview.example.test"}, trace),
        }
        self.repository = SimpleNamespace(projects=projects)
        self.settings = SimpleNamespace(console_login=console_login, jira_site="https://jira.example.test",
                                         review_preview_epic="TESTFLOW-5")
        self.user = SimpleNamespace(project_keys=scope, subject="reviewer", effective_roles=["reviewer"])

    def repo(self):
        self.trace.append(["repo"])
        return self.repository


def _ticket(key: str, status: str = "Human review", *, project: str = "DEMO", labels=None,
            blocked_by=None, parent_key=None) -> dict:
    return {
        "key": key, "project": project, "type": "Task", "summary": "Review item",
        "description": "A representative ticket.", "labels": labels or [], "parent_key": parent_key,
        "status": status, "blocked_by": blocked_by or [],
    }


def _owner_plan(kind: str, ticket_key: str) -> tuple[dict, dict]:
    ticket = _ticket(ticket_key, "Needs owner")
    plan: dict = {}
    if kind == "owner_escalated":
        ticket["labels"] = ["owner-escalated"]
    if kind == "owner_open_blocker":
        ticket["blocked_by"] = ["DEMO-2"]
        plan["blocker_statuses"] = []
    if kind == "owner_closed_blocker":
        ticket["blocked_by"] = ["DEMO-2"]
        plan["blocker_statuses"] = [{"key": "DEMO-2", "status": "Done"}]
    if kind in {"owner_asked", "owner_running", "owner_failed", "owner_timeout", "owner_lost",
                "owner_asked_after_triage", "owner_answered_before_ask"}:
        plan["asked_by_key"] = [{"ticket": ticket_key, "ts": STAMP}]
        if kind != "owner_asked":
            triage = {"ticket_key": ticket_key, "started_at": STAMP, "status": "completed",
                      "outcome": "answered", "ended_at": STAMP}
            if kind == "owner_running":
                triage.update(status="running", outcome=None, ended_at=None)
            elif kind in {"owner_failed", "owner_timeout", "owner_lost"}:
                triage["outcome"] = kind.removeprefix("owner_")
            elif kind == "owner_asked_after_triage":
                triage["ended_at"] = STAMP.replace(hour=10)
                plan["asked_by_key"][0]["ts"] = STAMP.replace(hour=11)
            elif kind == "owner_answered_before_ask":
                plan["asked_by_key"][0]["ts"] = STAMP.replace(hour=10)
            plan["triage_by_key"] = [triage]
    if kind == "owner_dev_blocked":
        plan["latest_runs"] = [{"ticket_key": ticket_key, "step": "dev", "outcome": "blocked",
                                "summary": "Waiting for an answer", "details": {}, "started_at": STAMP}]
    return ticket, plan


def _thread_plan(kind: str, ticket_key: str) -> tuple[dict | None, dict]:
    if kind == "recent_thread":
        return None, {
            "thread_event_dates": [{"ticket": "DEMO-9", "latest": STAMP}],
            "thread_events": [{"ticket": "DEMO-9", "ts": STAMP,
                               "data": {"action": "ask-ai", "text": "How should this work?"}}],
            "missing_thread_tickets": [{"key": "DEMO-9", "project": "DEMO", "summary": "Recent question",
                                        "status": "Needs owner"}],
        }
    if kind == "thread_event_and_run_history":
        return None, {
            "thread_event_dates": [{"ticket": "DEMO-6", "latest": STAMP.replace(hour=10)}],
            "thread_run_dates": [{"ticket": "DEMO-6", "latest": STAMP},
                                 {"ticket": "DEMO-7", "latest": STAMP.replace(hour=11)}],
            "thread_events": [
                {"ticket": "DEMO-6", "ts": STAMP.replace(hour=9),
                 "data": {"action": "ask-ai", "text": "What should change?"}},
                {"ticket": "DEMO-6", "ts": STAMP.replace(hour=10),
                 "data": {"action": "answer", "text": "Keep the retry."}},
            ],
            "thread_runs": [
                {"ticket_key": "DEMO-6", "step": "dev", "outcome": "blocked", "summary": "Need a decision",
                 "details": {}, "started_at": STAMP.replace(hour=8)},
                {"ticket_key": "DEMO-6", "step": "triage", "outcome": "escalated", "summary": "Unsafe",
                 "details": {"why": "Insufficient evidence"}, "started_at": STAMP.replace(hour=11)},
                {"ticket_key": "DEMO-7", "step": "triage", "outcome": "answered", "summary": "Ready to proceed",
                 "details": {}, "started_at": STAMP.replace(hour=12)},
            ],
            "missing_thread_tickets": [
                {"key": "DEMO-6", "project": "DEMO", "summary": "Thread with run history", "status": "Needs owner"},
                {"key": "DEMO-7", "project": "DEMO", "summary": "Thread from triage", "status": "Needs owner"},
            ],
        }
    if kind == "thread_recent_limit":
        return None, {
            "thread_event_dates": [
                {"ticket": f"DEMO-{index:02d}", "latest": STAMP - timedelta(minutes=index)}
                for index in range(51)
            ],
        }
    return _ticket(ticket_key), {
        "thread_event_dates": [{"ticket": ticket_key, "latest": STAMP}],
        "thread_events": [{"ticket": ticket_key, "ts": STAMP,
                           "data": {"action": "answer", "text": "Use the shared cache."}}],
    }


def _case_ticket_plan(kind: str, ticket_key: str) -> tuple[dict | None, dict]:
    if kind == "review_with_pr":
        ticket = _ticket(ticket_key)
        ticket["labels"] = ["require-human"]
        return ticket, {
            "dev_runs": [{"ticket_key": ticket_key, "pr_url": "https://github.example.test/org/demo/pull/12",
                          "summary": "Developer summary", "details": {}}],
            "latest_runs": [{"ticket_key": ticket_key, "step": "verify", "outcome": "passed",
                             "summary": "Checks passed", "details": {}, "started_at": STAMP}],
            "verify_runs": [{"ticket_key": ticket_key, "outcome": "passed", "summary": "Checks passed",
                             "details": {"verdict": "pass", "checks": ["unit"], "findings": []},
                             "started_at": STAMP}],
        }
    if kind == "review_without_pr":
        ticket = _ticket(ticket_key)
        ticket["labels"] = ["require-human"]
        return ticket, {}
    if kind.startswith("owner_"):
        return _owner_plan(kind, ticket_key)
    if kind == "closed_blocked_ticket":
        return _ticket(ticket_key, "In Progress", blocked_by=["DEMO-2"]), {"blocker_statuses": []}
    if kind == "parent_repository":
        ticket = _ticket(ticket_key)
        ticket["parent_key"] = "DEMO-EPIC"
        return ticket, {}
    if kind == "missing_project_config":
        ticket = _ticket(ticket_key)
        ticket["project"] = "UNCONFIGURED"
        return ticket, {}
    if kind == "pr_failure":
        return _ticket(ticket_key), {
            "dev_runs": [{"ticket_key": ticket_key, "pr_url": "https://github.example.test/org/demo/pull/0",
                          "summary": "Developer summary", "details": {}}],
        }
    if kind in {"testflow_preview_done", "testflow_preview_not_done"}:
        status = "Done" if kind == "testflow_preview_done" else "In Progress"
        return _ticket("TESTFLOW-1", project="TESTFLOW"), {"epics": [{"key": "TESTFLOW-5", "status": status}]}
    if kind in {"recent_thread", "thread_event_and_run_history", "thread_recent_limit", "thread_ai_answer"}:
        return _thread_plan(kind, ticket_key)
    return _ticket(ticket_key), {}


def _plan(kind: str) -> dict:
    plan: dict = {"metrics_tickets": []}
    if kind in {"empty", "unknown_project", "forbidden_project"}:
        return plan
    ticket, extra = _case_ticket_plan(kind, "DEMO-1")
    plan.update(extra)
    if ticket is not None:
        plan.update({
            "queue_candidates": [{"key": ticket["key"], "status": ticket["status"],
                                  "blocked_by": ticket.get("blocked_by", [])}],
            "tickets": [ticket],
        })
    return plan

CASES = [
    ("empty_unscoped", "empty", "", None, False),
    ("empty_project_filter", "empty", "DEMO", None, False),
    ("empty_role_scope", "empty", "", ("DEMO",), True),
    ("unknown_project_refusal", "unknown_project", "UNKNOWN", None, False),
    ("role_scope_refusal", "forbidden_project", "OTHER", ("DEMO",), True),
    ("review_with_pr_and_verify", "review_with_pr", "", None, False),
    ("review_without_pr", "review_without_pr", "", None, False),
    ("owner_question_without_triage", "owner_asked", "", None, False),
    ("owner_triage_running", "owner_running", "", None, False),
    ("owner_triage_failed", "owner_failed", "", None, False),
    ("owner_triage_timeout", "owner_timeout", "", None, False),
    ("owner_triage_lost", "owner_lost", "", None, False),
    ("owner_question_after_triage", "owner_asked_after_triage", "", None, False),
    ("owner_dev_run_blocked", "owner_dev_blocked", "", None, False),
    ("owner_escalated", "owner_escalated", "", None, False),
    ("owner_open_blocker", "owner_open_blocker", "", None, False),
    ("owner_closed_blocker", "owner_closed_blocker", "", None, False),
    ("blocked_ticket_closed_dependency", "closed_blocked_ticket", "", None, False),
    ("repository_uses_parent_epic", "parent_repository", "", None, False),
    ("ticket_without_project_config", "missing_project_config", "", None, False),
    ("github_gateway_failure", "pr_failure", "", None, False),
    ("testflow_preview_epic_done", "testflow_preview_done", "", None, False),
    ("testflow_preview_epic_open", "testflow_preview_not_done", "", None, False),
    ("recent_thread_without_selected_ticket", "recent_thread", "", None, False),
    ("thread_reviewer_answer", "thread_ai_answer", "", None, False),
    ("owner_answer_before_triage_ask", "owner_answered_before_ask", "", None, False),
    ("thread_event_and_run_history", "thread_event_and_run_history", "", None, False),
    ("thread_recent_limit_50", "thread_recent_limit", "", None, False),
]


def _gateway_for(plan: dict, trace: list):
    class Gateway:
        def pr(self, repository: str, number: int):
            trace.append(["github_pr", repository, number])
            if plan.get("raise_pr_error"):
                raise RuntimeError("gateway unavailable")
            return {"title": "Example change", "additions": 3, "deletions": 1, "changed_files": 2}

    def gateway(ctx, repository):
        trace.append(["github_gateway", repository])
        return Gateway()

    return gateway


def _capture_case(monkeypatch, spec: tuple):
    name, kind, project, scope, console_login = spec
    plan = _plan(kind)
    if kind == "pr_failure":
        plan["raise_pr_error"] = True
    trace: list[list] = []
    ctx = Context(plan, scope, console_login, trace)
    audits: list[dict] = []
    logs: list[str] = []
    metrics = {"metrics": {"people_needed_this_week": 0}, "projects": ["calculated"]}
    def calculate_metrics(*args, **kwargs):
        trace.append(["review_metrics"])
        return metrics

    monkeypatch.setattr(review_metrics, "calculate_review_metrics", calculate_metrics)

    class Logger:
        def warning(self, message: str, *args):
            rendered = message % args
            logs.append(rendered)
            trace.append(["log", rendered])

    def audit(*args):
        audits.append({"args": list(args)})
        trace.append(["audit", *args])

    def console_user(request, permission):
        trace.append(["console_user", permission])
        return ctx.user

    def optional_console_user(request):
        trace.append(["optional_console_user"])
        return ctx.user if scope is not None else None

    def repository_for_run(run, config):
        trace.append(["repository_for_run", config.name.lower()])
        return f"org/{config.name.lower()}"

    def ticket_epic(ticket):
        trace.append(["ticket_epic", ticket["key"]])
        return "DEMO-EPIC"

    api = ReviewQueueAPI(
        ctx, audit, repository_for_run, _gateway_for(plan, trace), ticket_epic, Logger(),
    )
    app = FastAPI()
    api.mount_human_reviews(app, console_user, optional_console_user)
    thread_requests: list[list[str]] = []
    load_threads = api.threads

    def record_thread_request(keys: list[str]):
        thread_requests.append(list(keys))
        trace.append(["threads", list(keys)])
        return load_threads(keys)

    api.threads = record_thread_request
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/v1/reviews")
    request = SimpleNamespace(url=SimpleNamespace(path="/v1/reviews"))
    try:
        output = {"status": 200, "body": endpoint(request, project)}
    except HTTPException as exc:
        output = {"status": exc.status_code, "detail": exc.detail}
    return {"name": name, "output": output, "queries": ctx.db.calls, "audits": audits, "logs": logs,
            "thread_requests": thread_requests, "trace": trace}


def test_human_reviews_matches_unchanged_route_characterization(monkeypatch):
    expected = [json.loads(line) for line in EXPECTED_PATH.read_text().splitlines() if line.strip()]
    assert [item["name"] for item in expected] == [case[0] for case in CASES]
    actual = [_capture_case(monkeypatch, spec) for spec in CASES]
    assert actual == expected


def test_human_review_thread_filters_keep_project_and_role_scope_order():
    assert _human_review_thread_filters("DEMO", ("OTHER",)) == (
        " AND ticket IN (SELECT key FROM tickets WHERE project=%s)", ("DEMO",),
        " AND project=%s", ("DEMO",),
    )
    assert _human_review_thread_filters("", ("DEMO",)) == (
        " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))", (["DEMO"],),
        " AND project=ANY(%s)", (["DEMO"],),
    )
    assert _human_review_thread_filters("", None) == ("", (), "", ())


def test_human_review_actions_preserve_item_and_pull_request_gates():
    review = {"labels": ["require-human"]}
    assert _human_review_actions("review_before_merge", object(), review) == [
        "approve", "reject", "rework", "ask-ai", "skip-gate",
    ]
    assert _human_review_actions("review_before_merge", None, {}) == ["rework", "ask-ai"]
    assert _human_review_actions("question_from_ai", None, {}) == ["answer", "ask-ai"]
    assert _human_review_actions("decision_needed", object(), {}) == ["answer", "ask-ai"]
    assert _human_review_actions("blocked", object(), {}) == ["ask-ai"]


def test_human_review_preview_is_only_exposed_after_configured_epic_done():
    ticket = {"project": "TESTFLOW"}
    assert _human_review_preview_url(ticket, Project("Testflow", {"preview": {"url": "https://preview.example.test"}}),
                                     {"TESTFLOW-5": "Done"}, "TESTFLOW-5") == "https://preview.example.test"
    assert _human_review_preview_url(ticket, Project("Testflow", {"preview": "https://preview.example.test"}),
                                     {"TESTFLOW-5": "In Progress"}, "TESTFLOW-5") == ""
    assert _human_review_preview_url({"project": "DEMO"}, Project("Demo", {"preview": "https://preview.example.test"}),
                                     {"TESTFLOW-5": "Done"}, "TESTFLOW-5") == ""


def test_recent_human_review_threads_include_only_known_tickets_with_messages():
    rows = HumanReviewThreads(
        data={"DEMO-1": [{"text": "Question"}], "DEMO-2": []},
        tickets={"DEMO-1": {"key": "DEMO-1", "summary": "Ticket"},
                 "DEMO-2": {"key": "DEMO-2", "summary": "Empty"}},
        recent_keys=["DEMO-1", "DEMO-2", "DEMO-3"],
    )
    assert _build_recent_human_review_threads(rows) == [
        {"key": "DEMO-1", "summary": "Ticket", "thread": [{"text": "Question"}]},
    ]


def test_recent_human_review_thread_query_caps_history_at_fifty():
    expected = [json.loads(line) for line in EXPECTED_PATH.read_text().splitlines() if line.strip()]
    result = next(item for item in expected if item["name"] == "thread_recent_limit_50")
    assert len(result["thread_requests"][0]) == 50
    assert "DEMO-49" in result["thread_requests"][0]
    assert "DEMO-50" not in result["thread_requests"][0]

