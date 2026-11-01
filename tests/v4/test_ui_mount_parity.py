"""Characterization of ``mount_ui``: routes, responses, refusals and call order.

Expected bodies were captured from the unchanged function (see
``tests/v4/fixtures/ui_mount_parity.jsonl``, one case per line). The table below is the
input inventory; the fixture is the literal output of that run.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.ui import (
    build_console_page,
    capacity_view,
    console,
    console_feature_script,
    gatus_services,
    help_document,
    javascript_asset,
    mount_console_pages,
    mount_feature_assets,
    mount_help,
    mount_logs_page,
    mount_notifications_page,
    mount_panel_assets,
    mount_service_status,
    mount_settings_page,
    mount_ticket_route,
    mount_ui,
    present_runs,
    require_console_user,
    service_rows,
    ticket_ancestors,
    ticket_detail,
)
from aiflow.v4.control import ui as ui_module

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/v4/fixtures/ui_mount_parity.jsonl"
SECRET = "s" * 32
OTHER_SECRET = "t" * 32
SHORT_SECRET = "short-secret-not-32-bytes!!!!!"  # 31 bytes
EXPIRES = 2_500_000_000
SUBJECT = "user@example.com"
WHEN = datetime(2026, 1, 1, tzinfo=timezone.utc)
FROZEN_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

ASSETS = (
    "/console-assets/provider-routing.js",
    "/console-assets/console-features.js",
    "/console-assets/licensing-notice.js",
    "/console-assets/owner-escalation.js",
    "/console-assets/release-readiness.js",
    "/console-assets/scale-proof.js",
    "/console-assets/flow-doctor.js",
    "/console-assets/run-inbox.js",
    "/console-assets/user-form-state.js",
    "/console-assets/quota-refresh-settings.js",
)
CONSOLE_PAGES = (
    "/dashboard", "/work", "/board", "/delivery", "/sa", "/business", "/review", "/stakeholder",
    "/machines", "/users", "/knowledge", "/backup", "/integrations", "/licensing",
    "/lead", "/projects", "/system-check", "/support", "/cc", "/security",
)
# cookie, user row, signing secret on the context. ``missing`` means no console_users row.
AUTH_ROWS = (
    ("no-cookie", None, "missing", SECRET),
    ("empty-cookie", "", "missing", SECRET),
    ("bad-cookie", "bad", "missing", SECRET),
    ("expired", "expired", "missing", SECRET),
    ("mfa", "mfa", "missing", SECRET),
    ("short-secret", "admin", "missing", SHORT_SECRET),
    ("wrong-secret", "admin", "missing", OTHER_SECRET),
    ("admin", "admin", "missing", SECRET),
    ("stakeholder", "stakeholder", "missing", SECRET),
    ("disabled", "admin", "disabled", SECRET),
    ("bad-role", "admin", "bad-role", SECRET),
    ("view-as", "admin-view-stakeholder", "missing", SECRET),
    ("promoted-without-totp", "stakeholder", "admin-no-totp", SECRET),
    ("promoted-with-totp", "stakeholder", "admin", SECRET),
)
GUARDED = ("/logs", "/notifications", "/settings")
HELP_ROWS = (
    ("redirect-without-site", "/help", "none"),
    ("root-without-site", "/help/", "none"),
    ("page-without-site", "/help/screens/dashboard.html", "none"),
    ("index", "/help/", "ready"),
    ("index-file", "/help/index.html", "ready"),
    ("dot", "/help/.", "ready"),
    ("screen", "/help/screens/dashboard.html", "ready"),
    ("directory", "/help/dir", "ready"),
    ("directory-slash", "/help/dir/", "ready"),
    ("empty-directory", "/help/empty", "ready"),
    ("missing", "/help/missing.html", "ready"),
    ("screens-directory", "/help/screens", "ready"),
    ("nested-text", "/help/nested/file.txt", "ready"),
    ("dotdot", "/help/..", "ready"),
    ("escape", "/help/../outside/secret.txt", "ready"),
    ("symlink", "/help/link", "ready"),
    ("encoded-dotdot", "/help/%2e%2e/%2e%2e/etc/passwd", "ready"),
    ("redirect-with-site", "/help", "ready"),
)


class ScriptDB:
    """Answers scripted statements and records every call in order."""

    def __init__(self, ones=None, alls=None):
        self.ones = ones or {}
        self.alls = alls or {}
        self.calls = []

    def one(self, sql, params=()):
        self.calls.append(("one", sql, params))
        return self._answer(self.ones, sql, params)

    def all(self, sql, params=()):
        self.calls.append(("all", sql, params))
        return self._answer(self.alls, sql, params)

    def _answer(self, table, sql, params):
        matches = [fragment for fragment in table if fragment in sql]
        if not matches:
            raise AssertionError(f"unexpected SQL: {sql!r}")
        value = table[max(matches, key=len)]
        result = value(params) if callable(value) else value
        return deepcopy(result) if result is not None else None


class GuardDB:
    def one(self, sql, params=()):
        raise AssertionError(f"unexpected db.one {sql!r}")

    def all(self, sql, params=()):
        raise AssertionError(f"unexpected db.all {sql!r}")


class Project:
    def __init__(self, repo):
        self.repo = repo
        self.seen = []

    def repo_for(self, ticket):
        self.seen.append({"key": ticket.get("key"), "epic": ticket.get("epic")})
        return self.repo


class Projects:
    def __init__(self, items):
        self.items = items
        self.gets = []

    def get(self, key):
        self.gets.append(key)
        return self.items.get(key)


class Ctx:
    def __init__(self):
        self.settings = SimpleNamespace(console_session_secret=SECRET, jira_site="https://jira.example")
        self.db = GuardDB()
        self.projects = Projects({})
        self.repo_calls = 0
        self.allow_repo = False

    def repo(self):
        if not self.allow_repo:
            raise AssertionError("unexpected repo()")
        self.repo_calls += 1
        return self


def _cookie(kind):
    if kind is None:
        return None
    if kind == "":
        return ""
    if kind == "bad":
        return "not-a-session"
    if kind == "expired":
        return issue_session(SUBJECT, "administrator", 1_000_000_000, SECRET)
    if kind == "mfa":
        return issue_session(SUBJECT, "administrator", EXPIRES, SECRET, mfa_pending=True)
    if kind == "admin":
        return issue_session(SUBJECT, "administrator", EXPIRES, SECRET)
    if kind == "stakeholder":
        return issue_session(SUBJECT, "stakeholder", EXPIRES, SECRET)
    if kind == "admin-view-stakeholder":
        return issue_session(SUBJECT, "administrator", EXPIRES, SECRET, view_as="stakeholder")
    raise KeyError(kind)


def _user_row(kind):
    admin = {"role": "administrator", "roles": ["administrator"], "project_keys": None,
             "disabled": False, "totp_enabled": True}
    if kind in (None, "missing"):
        return None
    if kind == "admin":
        return admin
    if kind == "disabled":
        return {**admin, "disabled": True}
    if kind == "bad-role":
        return {**admin, "role": "nope", "roles": ["nope"]}
    if kind == "admin-no-totp":
        return {**admin, "totp_enabled": False}
    if kind == "stakeholder":
        return {"role": "stakeholder", "roles": ["stakeholder"], "project_keys": ["JAIF"],
                "disabled": False, "totp_enabled": False}
    raise KeyError(kind)


def _ticket(**extra):
    row = {
        "key": "JAIF-1", "type": "Task", "parent_key": "", "project": "JAIF", "status": "To Do",
        "labels": [], "pr_url": "", "pr_state": "", "approved_at": None, "merged_at": None,
        "merge_sha": "", "merged_by": "", "merge_note": "",
    }
    row.update(extra)
    return row


def _parents(mapping):
    def answer(params):
        return mapping.get(params[0])
    return answer


def _timeline_row(ticket):
    return {"key": ticket["key"], "status": ticket["status"], "labels": list(ticket.get("labels") or []),
            "done_at": None}


def _freeze(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, tuple):
        return [_freeze(item) for item in value]
    if isinstance(value, list):
        return [_freeze(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _freeze(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(type(value))


def _db_log(db):
    return [{"op": op, "sql": sql, "params": _freeze(params)} for op, sql, params in db.calls]


def _capture(response):
    headers = {}
    for name in ("content-type", "cache-control", "location", "referrer-policy", "content-disposition"):
        if name in response.headers:
            headers[name] = response.headers[name]
    body = response.content
    item = {"status": response.status_code, "headers": headers, "length": len(body)}
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type or (body[:1] in (b"{", b"[") and len(body) < 200_000):
        try:
            item["json"] = response.json()
            return item
        except (ValueError, json.JSONDecodeError):
            pass
    if len(body) <= 500:
        item["text"] = response.text
    else:
        item["sha256"] = hashlib.sha256(body).hexdigest()
    return item


def _routes(app):
    found = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is None:
            continue
        found.append({
            "path": path,
            "methods": sorted(getattr(route, "methods", None) or []),
            "name": getattr(route, "name", None),
            "include_in_schema": bool(getattr(route, "include_in_schema", False)),
        })
    return found


def _site(tmp: Path) -> Path:
    root = tmp / "site"
    outside = tmp / "outside"
    (root / "screens").mkdir(parents=True)
    (root / "dir").mkdir()
    (root / "empty").mkdir()
    (root / "nested").mkdir()
    outside.mkdir()
    (root / "index.html").write_text("INDEX", encoding="utf-8")
    (root / "screens" / "dashboard.html").write_text("DASH", encoding="utf-8")
    (root / "dir" / "index.html").write_text("DIRINDEX", encoding="utf-8")
    (root / "nested" / "file.txt").write_text("NESTED", encoding="utf-8")
    (outside / "index.html").write_text("OUTSIDE", encoding="utf-8")
    (outside / "secret.txt").write_text("SECRET-FILE", encoding="utf-8")
    (root / "link").symlink_to(outside / "secret.txt")
    return root


@contextmanager
def _frozen_clock():
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW

    import aiflow.v4.control.timeline as timeline
    original = timeline.datetime
    timeline.datetime = Frozen
    try:
        yield
    finally:
        timeline.datetime = original


def _run_row(row):
    return {
        "id": row["id"], "step": row["step"], "account_id": row["account_id"], "provider": row["provider"],
        "model": row["model"], "node": row["node"], "fence": row["fence"], "status": row["status"],
        "outcome": row["outcome"], "error_kind": row["error_kind"], "started_at": row["started_at"],
        "ended_at": row["ended_at"], "tokens_in": row["tokens_in"], "tokens_out": row["tokens_out"],
        "cost_usd": row["cost_usd"], "branch": row["branch"], "pr_url": row["pr_url"],
        "head_sha": row["head_sha"], "summary": row["summary"],
    }


def _cases():
    rows = [{"name": "routes", "kind": "routes"}]
    rows.append({"name": "missing-path", "kind": "get", "path": "/no-such-page"})
    for path in ASSETS:
        rows.append({"name": f"asset:{path}", "kind": "get", "path": path})
    rows.append({"name": "redirect:/", "kind": "get", "path": "/"})
    for path in CONSOLE_PAGES:
        rows.append({"name": f"page:{path}", "kind": "get", "path": path})
    for path in GUARDED:
        for label, cookie, user, secret in AUTH_ROWS:
            rows.append({"name": f"auth:{path}:{label}", "kind": "auth", "path": path,
                         "cookie": cookie, "user": user, "secret": secret})
    for label, path, mode in HELP_ROWS:
        rows.append({"name": f"help:{label}", "kind": "help", "path": path, "site": mode})
    rows.extend(_ticket_cases())
    rows.extend(_service_cases())
    return rows


def _ticket_cases():
    chain = {f"T{index}": {"key": f"T{index}", "type": "Task", "parent_key": f"T{index + 1}"}
             for index in range(1, 9)}
    epic_chain = {
        "T1": {"key": "T1", "type": "Story", "parent_key": "T2"},
        "T2": {"key": "T2", "type": "Story", "parent_key": "EPIC-3"},
        "EPIC-3": {"key": "EPIC-3", "type": "Epic", "parent_key": ""},
    }
    run_a = {"id": UUID("12345678-1234-5678-1234-567812345678"), "step": "dev", "account_id": "acc",
             "provider": "codex", "model": "gpt", "node": "n1", "fence": 3, "status": "done",
             "outcome": "merged", "error_kind": "", "started_at": WHEN, "ended_at": WHEN + timedelta(hours=1),
             "tokens_in": 10, "tokens_out": 20, "cost_usd": Decimal("1.25"), "branch": "b",
             "pr_url": "https://github.com/o/r/pull/1", "head_sha": "abc", "summary": "did it"}
    run_b = {**run_a, "id": 7, "cost_usd": Decimal("0"), "step": "verify", "outcome": "pass"}
    events = [{"ts": "2026-01-01T00:00:00+00:00", "type": "note", "account": "a", "node": "n",
               "outcome": "", "reason": "because", "data": {"k": 1}}]
    writes = [{"ts": "2026-01-01T00:00:01+00:00", "target": "jira", "action": "comment", "actor": "a",
               "fence": 1, "accepted": True, "result": "ok"}]
    moves = [{"ts": "2026-01-01T00:00:02+00:00", "from_status": "To Do", "to_status": "In Progress",
              "cause": "claim", "detail": ""}]
    specs = [
        {"name": "unknown", "key": "MISSING-1", "found": False},
        {"name": "no-project", "ticket": _ticket(project="NONE"), "project": None, "jira": ""},
        {"name": "with-project", "ticket": _ticket(), "project": "org/aiflow", "jira": "https://jira.example"},
        {"name": "blank-epic-uses-epic-key", "ticket": _ticket(epic="", epic_key="EK", parent_key="T1"),
         "parents": {"T1": {"key": "T1", "type": "Story", "parent_key": ""}}, "project": "org/aiflow"},
        {"name": "epic-type-with-epic-key", "ticket": _ticket(key="EPIC-9", type="Epic", epic_key="OTHER"),
         "project": "org/aiflow"},
        {"name": "missing-parent", "ticket": _ticket(parent_key="GONE"), "parents": {}, "project": "org/aiflow"},
        {"name": "ancestor-cap", "ticket": _ticket(key="T0", parent_key="T1"), "parents": chain,
         "project": "org/aiflow"},
        {"name": "ancestor-epic", "ticket": _ticket(parent_key="T1"), "parents": epic_chain,
         "project": "org/aiflow"},
        {"name": "null-parent", "ticket": _ticket(parent_key=None), "project": "org/aiflow"},
        {"name": "capacity", "ticket": _ticket(), "project": "org/aiflow", "decision": {
            "account_id": "acc", "provider": "codex", "started_at": WHEN, "estimate": {"points": 2},
            "reason": "fit", "account_fit": {"ok": True}}},
        {"name": "capacity-falsy", "ticket": _ticket(), "project": "org/aiflow", "decision": {
            "account_id": "acc", "provider": "codex", "started_at": WHEN, "estimate": 0, "reason": 0,
            "account_fit": []}},
        {"name": "runs-and-pass-through", "ticket": _ticket(
            approved_at=WHEN, merged_at=WHEN + timedelta(seconds=90), merge_sha="abc", merged_by="bot",
            merge_note="shipped", pr_url="https://github.com/o/r/pull/1", pr_state="merged"),
         "project": "org/aiflow", "runs": [run_a, run_b], "events": events, "writes": writes, "moves": moves,
         "decision": {"account_id": "acc", "provider": "codex", "started_at": WHEN, "estimate": {},
                      "reason": "", "account_fit": {}}},
        {"name": "null-cost", "ticket": _ticket(), "project": "org/aiflow", "runs": [{**run_a, "cost_usd": None}]},
        {"name": "null-assigned-at", "ticket": _ticket(), "project": "org/aiflow", "decision": {
            "account_id": "acc", "provider": "codex", "started_at": None, "estimate": {"points": 1},
            "reason": "fit", "account_fit": {"ok": True}}},
        {"name": "timeline-miss", "ticket": _ticket(), "project": "org/aiflow", "timeline": False,
         "jira": "https://jira.example/"},
        {"name": "merge-before-approval", "ticket": _ticket(
            approved_at=WHEN, merged_at=WHEN - timedelta(seconds=1)), "project": "org/aiflow"},
        {"name": "merge-at-approval", "ticket": _ticket(approved_at=WHEN, merged_at=WHEN), "project": "org/aiflow"},
        {"name": "approved-not-merged", "ticket": _ticket(approved_at=WHEN, merged_at=None), "project": "org/aiflow"},
    ]
    cases = []
    for spec in specs:
        label = spec["name"]
        cases.append({"kind": "ticket", **spec, "name": f"ticket:{label}"})
    return cases


def _service_cases():
    rich = [
        {"name": "empty-results", "group": "edge", "results": []},
        {"name": "blank-results", "group": "edge", "results": ""},
        {"name": "no-results", "group": "edge"},
        {"group": "nameless", "results": [{"success": True, "duration": 1_000_000}]},
        {"name": "last-wins", "group": "edge", "results": [
            {"success": False, "duration": 9_000_000, "errors": ["first"]},
            {"success": True, "duration": 1_500_000, "errors": ["last"]}]},
        {"name": "just-under", "group": "edge", "results": [{"success": True, "duration": 999_999}]},
        {"name": "truthy-success", "group": "edge", "results": [{"success": 1, "duration": 1_999_999, "errors": None}]},
        {"name": "zero-success", "group": "edge", "results": [{"success": 0, "duration": 0, "errors": []}]},
        {"name": "missing-fields", "group": "edge", "results": [{"success": False}]},
        {"name": "none-duration", "group": "edge", "results": [{"success": True, "duration": None, "errors": ["e"]}]},
        {"name": "bare", "results": [{}]},
    ]
    return [
        {"name": "services:unreachable", "kind": "services", "raise": "down"},
        {"name": "services:bad-json", "kind": "services", "bad_json": True},
        {"name": "services:empty", "kind": "services", "json": []},
        {"name": "services:edges", "kind": "services", "json": rich},
        {"name": "services:not-a-list", "kind": "services", "json": {"name": "nope"}},
        {"name": "services:results-scalar", "kind": "services", "json": [{"name": "x", "results": "abc"}]},
    ]


def _prepare_ticket(ctx, case):
    ticket = case.get("ticket")
    ones = {}
    alls = {}
    if not case.get("found", True):
        ones["SELECT * FROM tickets"] = None
    else:
        ones["SELECT * FROM tickets"] = ticket
        ones["parent_key FROM tickets"] = _parents(case.get("parents") or {})
        ones["done_at FROM tickets"] = _timeline_row(ticket) if case.get("timeline", True) else None
        alls["LIMIT 20"] = [_run_row(row) for row in case.get("runs", [])]
        ones["capacity"] = case.get("decision")
        alls["LIMIT 80"] = list(case.get("events", []))
        alls["write_audit"] = list(case.get("writes", []))
        alls["heartbeat_at"] = []
        alls["type = ANY"] = []
        alls["ticket_moves"] = list(case.get("moves", []))
    ctx.db = ScriptDB(ones, alls)
    ctx.allow_repo = True
    ctx.repo_calls = 0
    project = Project(case["project"]) if case.get("project") else None
    items = {ticket["project"]: project} if ticket and project else {}
    ctx.projects = Projects(items)
    ctx.settings.jira_site = case.get("jira", "https://jira.example")
    ctx.settings.console_session_secret = SECRET


def _observe_get(client, path, cookie=None):
    client.cookies.clear()
    kwargs = {}
    if cookie is not None:
        kwargs["cookies"] = {"aiflow4_session": cookie}
    return _capture(client.get(path, **kwargs))


def _observe_auth(client, ctx, case):
    auth_calls = []

    def verify(token, secret, now=None, allow_pending=False):
        auth_calls.append({"fn": "verify_session", "token": token, "secret": secret, "allow_pending": allow_pending})
        return ui_module_verify(token, secret, now=now, allow_pending=allow_pending)

    def resolve(db, claims, allow_pending=False):
        auth_calls.append({"fn": "resolve_session_user", "subject": claims.subject, "allow_pending": allow_pending})
        return ui_module_resolve(db, claims, allow_pending=allow_pending)

    def satisfies(role, required):
        roles = [role] if isinstance(role, str) else list(role)
        auth_calls.append({"fn": "role_satisfies", "roles": roles, "required": sorted(required)})
        return ui_module_satisfies(role, required)

    ctx.db = ScriptDB({"console_users": _user_row(case["user"])})
    ctx.allow_repo = False
    ctx.settings.console_session_secret = case["secret"]
    ctx.settings.jira_site = "https://jira.example"
    original = (ui_module.verify_session, ui_module.resolve_session_user, ui_module.role_satisfies)
    ui_module.verify_session = verify
    ui_module.resolve_session_user = resolve
    ui_module.role_satisfies = satisfies
    try:
        observed = _observe_get(client, case["path"], _cookie(case["cookie"]))
    finally:
        ui_module.verify_session, ui_module.resolve_session_user, ui_module.role_satisfies = original
    observed["auth"] = auth_calls
    observed["db"] = _db_log(ctx.db)
    return observed


ui_module_verify = ui_module.verify_session
ui_module_resolve = ui_module.resolve_session_user
ui_module_satisfies = ui_module.role_satisfies


def run_all():
    ctx = Ctx()
    app = FastAPI()
    mount_ui(app, ctx)
    site_root = None
    rows = []
    with TemporaryDirectory() as tmp, TestClient(app, follow_redirects=False, raise_server_exceptions=False) as client:
        site_root = _site(Path(tmp))
        with _frozen_clock():
            for case in _cases():
                rows.append({"name": case["name"], "observed": _execute(app, client, ctx, case, site_root)})
    return rows


def _execute(app, client, ctx, case, site_root):
    kind = case["kind"]
    if kind == "routes":
        return _routes(app)
    if kind == "get":
        ctx.db = GuardDB()
        ctx.allow_repo = False
        return _observe_get(client, case["path"])
    if kind == "auth":
        return _observe_auth(client, ctx, case)
    if kind == "help":
        ctx.db = GuardDB()
        ctx.allow_repo = False

        def site_dir():
            return None if case["site"] == "none" else site_root

        import aiflow.v4.core.docsite as docsite
        original = docsite.site_dir
        docsite.site_dir = site_dir
        try:
            return _observe_get(client, case["path"])
        finally:
            docsite.site_dir = original
    if kind == "ticket":
        _prepare_ticket(ctx, case)
        observed = _observe_get(client, f"/v1/tickets/{case.get('key', case.get('ticket', {}).get('key', 'JAIF-1'))}")
        observed["db"] = _db_log(ctx.db)
        observed["repo_calls"] = ctx.repo_calls
        observed["project_gets"] = list(ctx.projects.gets)
        observed["repo_for"] = [item for project in ctx.projects.items.values() if project for item in project.seen]
        return observed
    if kind == "services":
        ctx.db = GuardDB()
        ctx.allow_repo = False
        http_calls = []

        def fake_get(*args, **kwargs):
            http_calls.append({"args": list(args), "timeout": kwargs.get("timeout")})
            if case.get("raise"):
                raise RuntimeError(case["raise"])

            class Response:
                def json(self):
                    if case.get("bad_json"):
                        raise ValueError("bad json")
                    return case["json"]

            return Response()

        original = ui_module.httpx.get
        ui_module.httpx.get = fake_get
        try:
            observed = _observe_get(client, "/v1/services")
        finally:
            ui_module.httpx.get = original
        observed["http"] = http_calls
        return observed
    raise KeyError(kind)


_TOKEN = re.compile(r"^eyJ[\w-]+\.[\w-]+\.[\w-]+$")


def _scrub(value):
    """Replace signed session tokens by a placeholder: the test mints them, and the fixture must hold no token-shaped text."""
    if isinstance(value, str):
        return "<session-token>" if _TOKEN.match(value) else value
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: "<test-secret>" if key == "secret" and isinstance(item, str) else _scrub(item)
                for key, item in value.items()}
    return value


def _expected():
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [(row["name"], row["observed"]) for row in rows]


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_mount_ui_matches_recorded_behavior":
        return
    pairs = _expected()
    metafunc.parametrize(("name", "expected"), pairs, ids=[name for name, _ in pairs])


@pytest.fixture(scope="module")
def observed_by_name():
    return {row["name"]: _scrub(row["observed"]) for row in run_all()}


def test_mount_ui_case_inventory(observed_by_name):
    assert list(observed_by_name) == [name for name, _ in _expected()]


def test_mount_ui_matches_recorded_behavior(name, expected, observed_by_name):
    assert observed_by_name[name] == expected


def _mounted_paths(app):
    skip = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    return [route.path for route in app.routes if getattr(route, "path", None) not in skip]


def test_javascript_asset_serves_the_file_without_caching(tmp_path):
    script = tmp_path / "panel.js"
    script.write_text("var panel = 1;\n", encoding="utf-8")
    response = javascript_asset(script)
    assert response.path == script
    assert response.media_type == "application/javascript"
    assert response.headers["cache-control"] == "no-cache"


def test_console_feature_script_joins_licensing_and_provider_routing():
    from aiflow.v4.control.ui import HERE
    licensing = (HERE / "licensing_notice.js").read_text(encoding="utf-8")
    routing = (HERE / "provider-routing.js").read_text(encoding="utf-8")
    assert console_feature_script() == licensing + "\n" + routing


def test_mount_feature_assets_registers_the_three_feature_scripts_in_order():
    app = FastAPI()
    mount_feature_assets(app)
    assert _mounted_paths(app) == [
        "/console-assets/provider-routing.js",
        "/console-assets/console-features.js",
        "/console-assets/licensing-notice.js",
    ]


def test_mount_panel_assets_registers_panel_scripts_in_order():
    app = FastAPI()
    mount_panel_assets(app)
    assert _mounted_paths(app) == [
        "/console-assets/owner-escalation.js",
        "/console-assets/release-readiness.js",
        "/console-assets/scale-proof.js",
        "/console-assets/flow-doctor.js",
        "/console-assets/run-inbox.js",
        "/console-assets/user-form-state.js",
        "/console-assets/quota-refresh-settings.js",
    ]


def test_console_replaces_only_the_first_licensing_script_tag():
    raw = build_console_page()
    response = console()
    needle = '<script src="/console-assets/licensing-notice.js"></script>'
    replacement = '<script src="/console-assets/console-features.js"></script>'
    assert response.headers["cache-control"] == "no-store"
    assert response.body.decode() == raw.replace(needle, replacement, 1)


def test_mount_console_pages_redirects_root_and_skips_guarded_paths():
    app = FastAPI()
    mount_console_pages(app)
    paths = _mounted_paths(app)
    assert paths[0] == "/"
    assert "/help" not in paths and "/notifications" not in paths and "/settings" not in paths
    assert "/logs" not in paths and "/dashboard" in paths and paths[-1] == "/security"


def test_require_console_user_refuses_a_missing_or_unprivileged_session():
    app = FastAPI()
    ctx = SimpleNamespace(settings=SimpleNamespace(console_session_secret=SECRET),
                          db=ScriptDB({"console_users": None}))

    @app.get("/probe")
    def probe(request: Request):
        require_console_user(request, ctx, "view.logs")
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    missing = client.get("/probe")
    assert missing.status_code == 401
    assert missing.json()["detail"] == "authenticated console session required"
    client.cookies.set("aiflow4_session", issue_session(SUBJECT, "stakeholder", EXPIRES, SECRET))
    refused = client.get("/probe")
    assert refused.status_code == 403
    assert refused.json()["detail"] == "not available for your role"


def test_mount_logs_page_asks_for_the_logs_permission():
    app = FastAPI()
    ctx = SimpleNamespace(settings=SimpleNamespace(console_session_secret=SECRET), db=GuardDB())
    mount_logs_page(app, ctx)
    route = next(route for route in app.routes if getattr(route, "path", None) == "/logs")
    assert route.name == "logs" and route.include_in_schema is False
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/logs").status_code == 401


def test_help_document_serves_a_page_inside_the_site_and_refuses_the_rest(tmp_path):
    root = tmp_path / "site"
    (root / "dir").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "index.html").write_text("INDEX", encoding="utf-8")
    (root / "dir" / "index.html").write_text("DIRINDEX", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET-FILE", encoding="utf-8")
    (root / "link").symlink_to(outside)

    with pytest.raises(HTTPException) as missing_site:
        help_document("index.html", None)
    assert missing_site.value.status_code == 404
    assert missing_site.value.detail == "the documentation site is not installed"

    index = help_document("", root)
    assert index.path == (root / "index.html").resolve()
    assert index.headers["cache-control"] == "no-cache"
    assert help_document("dir", root).path == (root / "dir" / "index.html").resolve()

    with pytest.raises(HTTPException) as missing_page:
        help_document("empty", root)
    assert missing_page.value.detail == "no such documentation page"
    with pytest.raises(HTTPException) as escaped:
        help_document("link", root)
    assert escaped.value.detail == "no such documentation page"


def test_mount_help_redirects_the_help_root_and_keeps_the_page_handler_name():
    app = FastAPI()
    mount_help(app)
    names = {route.path: route.name for route in app.routes if getattr(route, "path", "").startswith("/help")}
    assert names == {"/help": "help_root", "/help/{path:path}": "help_site"}


def test_mount_notifications_page_uses_the_notifications_permission():
    app = FastAPI()
    ctx = SimpleNamespace(settings=SimpleNamespace(console_session_secret=SECRET), db=GuardDB())
    mount_notifications_page(app, ctx)
    route = next(route for route in app.routes if getattr(route, "path", None) == "/notifications")
    assert route.name == "notifications"
    client = TestClient(app, raise_server_exceptions=False)
    refused = client.get("/notifications")
    assert refused.status_code == 401
    assert refused.json()["detail"] == "authenticated console session required"


def test_mount_settings_page_uses_the_console_manage_permission():
    app = FastAPI()
    ctx = SimpleNamespace(settings=SimpleNamespace(console_session_secret=SECRET), db=GuardDB())
    mount_settings_page(app, ctx)
    route = next(route for route in app.routes if getattr(route, "path", None) == "/settings")
    assert route.name == "settings_page"
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/settings").json()["detail"] == "authenticated console session required"


def test_ticket_ancestors_stops_at_seven_parents_or_a_missing_row():
    rows = {f"T{index}": {"key": f"T{index}", "type": "Task", "parent_key": f"T{index + 1}"}
            for index in range(1, 10)}
    db = ScriptDB({"parent_key FROM tickets": lambda params: rows.get(params[0])})
    ancestors = ticket_ancestors(db, {"key": "T0", "type": "Task", "parent_key": "T1"})
    assert [item["key"] for item in ancestors] == [f"T{index}" for index in range(8)]
    assert [params[0] for _op, _sql, params in db.calls] == [f"T{index}" for index in range(1, 8)]

    empty = ScriptDB({"parent_key FROM tickets": None})
    assert ticket_ancestors(empty, {"key": "T0", "parent_key": "GONE"}) == [{"key": "T0", "parent_key": "GONE"}]
    assert len(empty.calls) == 1
    untouched = ScriptDB({})
    assert ticket_ancestors(untouched, {"key": "T0", "parent_key": None}) == [{"key": "T0", "parent_key": None}]
    assert untouched.calls == []


def test_present_runs_stringifies_ids_and_floats_costs():
    row = {"id": UUID("12345678-1234-5678-1234-567812345678"), "cost_usd": Decimal("1.25")}
    assert present_runs([row]) == [{"id": "12345678-1234-5678-1234-567812345678", "cost_usd": 1.25}]
    assert row["id"] == "12345678-1234-5678-1234-567812345678"


def test_capacity_view_drops_falsy_estimate_reason_and_fit():
    assert capacity_view(None) is None
    assert capacity_view({"account_id": "acc", "provider": "codex", "started_at": WHEN,
                          "estimate": {"points": 2}, "reason": "fit", "account_fit": {"ok": True}}) == {
        "account": "acc", "provider": "codex", "assigned_at": "2026-01-01T00:00:00+00:00",
        "estimate": {"points": 2}, "reason": "fit", "account_fit": {"ok": True}}
    assert capacity_view({"account_id": "acc", "provider": "codex", "started_at": WHEN,
                          "estimate": 0, "reason": 0, "account_fit": []}) == {
        "account": "acc", "provider": "codex", "assigned_at": "2026-01-01T00:00:00+00:00",
        "estimate": {}, "reason": "", "account_fit": {}}


def test_ticket_detail_refuses_an_unknown_key_before_any_later_query():
    db = ScriptDB({"SELECT * FROM tickets": None})
    ctx = SimpleNamespace(db=db)
    with pytest.raises(HTTPException) as caught:
        ticket_detail(ctx, "MISSING-1")
    assert caught.value.status_code == 404
    assert caught.value.detail == "unknown ticket"
    assert len(db.calls) == 1


def test_mount_ticket_route_keeps_the_schema_and_handler_name():
    app = FastAPI()
    mount_ticket_route(app, object())
    route = next(route for route in app.routes if getattr(route, "path", None) == "/v1/tickets/{key}")
    assert route.name == "ticket"
    assert route.include_in_schema is True
    assert sorted(route.methods) == ["GET"]


def test_service_rows_keeps_the_last_result_and_truncates_milliseconds():
    rows = service_rows([
        {"name": "edge", "group": "g", "results": [
            {"success": False, "duration": 9_000_000, "errors": ["first"]},
            {"success": True, "duration": 1_500_000, "errors": ["last"]}]},
        {"name": "under", "group": "g", "results": [{"success": True, "duration": 999_999}]},
        {"results": ""},
    ])
    assert rows == [
        {"name": "edge", "group": "g", "up": True, "ms": 1, "errors": ["last"]},
        {"name": "under", "group": "g", "up": True, "ms": 0, "errors": []},
        {"name": None, "group": None, "up": False, "ms": 0, "errors": []},
    ]


def test_gatus_services_reports_the_monitor_as_unreachable(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(ui_module.httpx, "get", boom)
    assert gatus_services() == {"services": [], "error": "gatus unreachable"}


def test_mount_service_status_keeps_the_services_handler_in_the_schema():
    app = FastAPI()
    mount_service_status(app)
    route = next(route for route in app.routes if getattr(route, "path", None) == "/v1/services")
    assert route.name == "services"
    assert route.include_in_schema is True
    assert route.endpoint.__doc__ == "Active checks from Gatus (name, group, up, response time)."
