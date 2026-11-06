"""Characterize the existing GET /v1/reviews mount and its observable call order."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control import review_queue_api


FIXTURE = Path(__file__).parent / "fixtures" / "human_review_mount_parity.jsonl"


def ticket(key="T-1", **changes):
    item = {"key": key, "project": "TESTWP", "type": "Story", "summary": "Sample task",
            "description": "A small review example", "labels": [], "parent_key": None,
            "status": "Human review", "blocked_by": []}
    return item | changes


def case(name, **changes):
    return {"name": name, "project": "", "console_login": False,
            "user": {"subject": "reviewer", "project_keys": None, "effective_roles": ["human_reviewer"]},
            "selected": ["T-1"], "tickets": [ticket()], "rows": {}, "kinds": {},
            "projects": {"TESTWP": {"repository": "demo/tasks", "preview": None}},
            "threads": {}, "metrics": {"metrics": {"count": 0}, "projects": ["TESTWP"]}} | changes


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


CASES = [
    case("console_login", console_login=True),
    case("optional_console_user"),
    case("anonymous_project_query", project="TESTWP", user=None),
    case("project_scope_denied", project="TESTWP", user={"subject": "reviewer", "project_keys": ["OTHER"],
                                                             "effective_roles": ["human_reviewer"]}),
    case("empty_role_scope_denied", project="TESTWP", user={"subject": "reviewer", "project_keys": [],
                                                               "effective_roles": ["human_reviewer"]}),
    case("unknown_project_error", project="UNKNOWN", data_error=[400, "unknown project filter"]),
    case("empty_selected_keys", selected=[], tickets=[]),
    case("needs_owner_without_keys", tickets=[]),
    case("needs_owner_triage", tickets=[ticket(status="Needs owner")], rows={
        "asked": [{"ticket": "T-1", "ts": "2026-01-02T00:00:00+00:00"}],
        "triage": [{"ticket_key": "T-1", "status": "completed", "outcome": "answered"}]}),
    case("explicit_project_filter", project="TESTWP"),
    case("allowed_project_filter_with_scope", project="TESTWP", user={"subject": "reviewer",
        "project_keys": ["TESTWP"], "effective_roles": ["human_reviewer"]}),
    case("role_project_scope", user={"subject": "reviewer", "project_keys": ["TESTWP", "OTHER"],
                                     "effective_roles": ["human_reviewer"]}),
    case("unscoped_threads"),
    case("recent_thread_merge_and_missing_ticket", tickets=[], rows={
        "thread_events": [{"ticket": "OLD-1", "latest": "2026-01-02T00:00:00+00:00"}],
        "thread_runs": [{"ticket": "OLD-1", "latest": "2026-01-03T00:00:00+00:00"}],
        "missing_tickets": [{"key": "OLD-1", "project": "TESTWP", "summary": "Old thread", "status": "Needs owner"}]},
        threads={"OLD-1": [{"kind": "question", "author": "Reviewer", "text": "Need a decision.",
                            "created_at": "2026-01-03T00:00:00+00:00"}]}),
    case("recent_thread_limit_50", tickets=[], rows={"thread_runs": [
        {"ticket": f"OLD-{n:02}", "latest": f"2026-01-{n:02}T00:00:00+00:00"} for n in range(1, 53)]}),
    case("dev_repository_and_pull_request", tickets=[ticket(parent_key="EPIC-1")], rows={
        "dev_runs": [{"ticket_key": "T-1", "pr_url": "https://code.test/pull/42", "summary": "Built", "details": {}}]},
        gateway_result={"title": "Example change", "additions": 2, "deletions": 1, "changed_files": 1}),
    case("parent_ticket_epic_lookup", tickets=[ticket(parent_key="EPIC-1")]),
    case("missing_project_configuration", projects={}),
    case("no_pull_request_hides_actions", tickets=[ticket()], kinds={"T-1": "review_before_merge"}),
    case("pull_request_number_boundary", rows={"dev_runs": [
        {"ticket_key": "T-1", "pr_url": "https://code.test/pull/042/", "summary": "Built", "details": {}}]}),
    case("malformed_pull_request_url", rows={"dev_runs": [
        {"ticket_key": "T-1", "pr_url": "https://code.test/pull/42?tab=files", "summary": "Built", "details": {}}]}),
    case("gateway_failure", rows={"dev_runs": [
        {"ticket_key": "T-1", "pr_url": "https://code.test/pull/42", "summary": "Built", "details": {}}]},
        gateway_error="provider unavailable"),
    case("preview_dictionary_allowed", tickets=[ticket(project="TESTFLOW")],
        projects={"TESTFLOW": {"repository": "demo/tasks", "preview": {"url": "https://preview.example.invalid"}}},
        rows={"epics": [{"key": "TESTFLOW-5", "status": "Done"}]}),
    case("preview_string_allowed", tickets=[ticket(project="TESTFLOW")],
        projects={"TESTFLOW": {"repository": "demo/tasks", "preview": "https://preview.example.invalid"}},
        rows={"epics": [{"key": "TESTFLOW-5", "status": "Done"}]}),
    case("preview_hidden_for_other_project", projects={"TESTWP": {"repository": "demo/tasks",
        "preview": "https://preview.example.invalid"}}),
    case("preview_hidden_until_epic_done", tickets=[ticket(project="TESTFLOW")],
        projects={"TESTFLOW": {"repository": "demo/tasks", "preview": "https://preview.example.invalid"}},
        rows={"epics": [{"key": "TESTFLOW-5", "status": "In Progress"}]}),
    case("blocker_status_boundaries", tickets=[ticket("OPEN", blocked_by=["OPEN-B", "CLOSED-B", "MISSING-B"]),
                                                  ticket("CLOSED", blocked_by=["CLOSED-B"])],
        rows={"blockers": [{"key": "OPEN-B", "status": "In Progress"}, {"key": "CLOSED-B", "status": "Closed"}]},
        selected=["OPEN", "CLOSED"], kinds={"OPEN": "blocked", "CLOSED": "decision_needed"}),
    case("all_item_action_kinds_and_skip_gate", tickets=[
        ticket("REVIEW", labels=[]), ticket("REQUIRED", labels=["require-human"]),
        ticket("QUESTION", status="Needs owner"), ticket("DECISION", status="Needs owner"),
        ticket("BLOCKED", status="Needs owner")],
        selected=["REVIEW", "REQUIRED", "QUESTION", "DECISION", "BLOCKED"],
        kinds={"REVIEW": "review_before_merge", "REQUIRED": "review_before_merge", "QUESTION": "question_from_ai",
               "DECISION": "decision_needed", "BLOCKED": "blocked"},
        rows={"dev_runs": [{"ticket_key": "REVIEW", "pr_url": "https://code.test/pull/1", "details": {}},
                            {"ticket_key": "REQUIRED", "pr_url": "https://code.test/pull/2", "details": {}}]}),
]


class FakeDatabase:
    def __init__(self, rows, events):
        self.rows = rows
        self.events = events

    def all(self, sql, params=()):
        if "SELECT key, project, type, summary" in sql:
            tag = "tickets"
        elif "WHERE key=%s" in sql:
            tag = "epics"
        elif "SELECT key,status FROM tickets" in sql:
            tag = "blockers"
        elif "pr_url<>''" in sql:
            tag = "dev_runs"
        elif "step='verify'" in sql:
            tag = "verify_runs"
        elif "DISTINCT ON (ticket_key) ticket_key,step" in sql:
            tag = "latest_runs"
        elif "DISTINCT ON (ticket) ticket,ts" in sql:
            tag = "asked"
        elif "DISTINCT ON (ticket_key) ticket_key,started_at,status" in sql:
            tag = "triage"
        elif "max(ts) AS latest" in sql:
            tag = "thread_events"
        elif "max(started_at) AS latest" in sql:
            tag = "thread_runs"
        elif "SELECT key,project,summary,status FROM tickets" in sql:
            tag = "missing_tickets"
        else:
            raise AssertionError("unclassified review query")
        self.events.append(["db", tag, normalize(params)])
        return self.rows.get(tag, [])


class FakeProject:
    def __init__(self, data, events):
        self.repository = data["repository"]
        self.environments = {"preview": data["preview"]}
        self.events = events

    def repo_for(self, item):
        self.events.append(["repo_for", item["key"]])
        return self.repository


class FakeData:
    def __init__(self, spec, events):
        self.spec, self.events = spec, events

    def __call__(self, project, scope, selected):
        self.events.append(["data", project, scope, selected])
        if self.spec.get("data_error"):
            raise HTTPException(*self.spec["data_error"])
        return self.spec["metrics"]


class FakeGateway:
    def __init__(self, spec, events, repository):
        self.spec, self.events, self.repository = spec, events, repository
        events.append(["github_gateway", repository])

    def pr(self, project_name, number):
        self.events.append(["pr", project_name, number])
        if self.spec.get("gateway_error"):
            raise RuntimeError(self.spec["gateway_error"])
        return self.spec.get("gateway_result")


def observe_case(spec, after_mount=None):
    events = []
    user = SimpleNamespace(**spec["user"]) if spec["user"] else None
    projects = {key: FakeProject(value, events) for key, value in spec["projects"].items()}
    repo = SimpleNamespace(projects=projects)
    db = FakeDatabase(spec["rows"] | {"tickets": spec["tickets"]}, events)
    ctx = SimpleNamespace(settings=SimpleNamespace(console_login=spec["console_login"],
                                                   jira_site="https://jira.example.invalid/",
                                                   review_preview_epic="TESTFLOW-5"), db=db,
                          repo=lambda: (events.append(["repo"]), repo)[1])

    def auth(label):
        def check(request, *args):
            events.append([label, *args])
            return user
        return check

    queue = review_queue_api.ReviewQueueAPI(ctx, lambda *args: events.append(["audit", *args]),
        lambda run, project: events.append(["repository_for_run", run["ticket_key"]]) or project.repository,
        lambda _ctx, repository: FakeGateway(spec, events, repository),
        lambda item: events.append(["ticket_epic", item["key"]]) or "Epic",
        SimpleNamespace(warning=lambda message, *args: events.append(["warning", message % args])))
    queue.queue_keys = lambda project, scope: (events.append(["queue_keys", project, scope]) or spec["selected"])
    queue.data = FakeData(spec, events)
    queue.threads = lambda keys: (events.append(["threads", keys]) or spec["threads"])
    queue.item_kind = lambda item, latest, question: (
        events.append(["item_kind", item["key"], item["_has_open_blocker"], latest, question])
        or spec["kinds"].get(item["key"], "review_before_merge"))
    app = SimpleNamespace(routes=[])

    def get(path, **options):
        def register(handler):
            app.routes.append(("GET", path, options.get("name", handler.__name__), handler))
            return handler
        return register

    app.get = get
    queue.mount_human_reviews(app, auth("console_user"), auth("optional_console_user"))
    if after_mount:
        after_mount()
    route = app.routes[0]
    request = SimpleNamespace(url=SimpleNamespace(path="/v1/reviews"))
    try:
        response = route[3](request, project=spec["project"])
        result = {"response": response}
    except HTTPException as exc:
        result = {"error": {"status": exc.status_code, "detail": exc.detail}}
    return {"route": list(route[:3]), **result, "events": events}


@pytest.mark.parametrize("spec", CASES, ids=lambda spec: spec["name"])
def test_human_review_mount_matches_pre_refactor_golden(spec):
    expected = {json.loads(line)["case"]: json.loads(line)["expected"] for line in FIXTURE.read_text().splitlines()}
    assert normalize(observe_case(spec)) == expected[spec["name"]]


def test_route_reads_source_module_regex_after_registration(monkeypatch):
    from aiflow.v4.control import review_queue_mount

    assert review_queue_mount.review_queue_api is review_queue_api
    original = review_queue_api.re
    searched = []
    regex = SimpleNamespace(search=lambda pattern, value: (searched.append(value), original.search(pattern, value))[1])
    spec = next(item for item in CASES if item["name"] == "dev_repository_and_pull_request")
    observed = observe_case(spec, lambda: monkeypatch.setattr(review_queue_api, "re", regex))
    assert searched == ["https://code.test/pull/42"], (searched, observed)
    assert observed["response"]["tickets"][0]["pr"]["number"] == 42
