"""Release readiness of a Jira version: every ticket currently in the version is Done, verified, out of rework and
not blocked by an open release-qa Bug; the reasons reach /v1/state (Command Center) and Business."""
import json
import uuid

from conftest import add_ticket
from test_business import DOM, STATE, business
from test_console import run_script
from aiflow.v4.control import release_readiness
from aiflow.v4.control.loops import jira_sync

VERSION = {"id": "100", "name": "1.0", "released": False, "release_date": None}


def issue(key: str, status: str = "Done", versions=("100",), labels=(), type_="Story", blocked_by=()) -> dict:
    return {"key": key, "fields": {
        "summary": f"Summary of {key}", "status": {"name": status}, "issuetype": {"name": type_},
        "labels": list(labels), "fixVersions": [{"id": v} for v in versions],
        "issuelinks": [{"type": {"inward": "is blocked by"}, "inwardIssue": {"key": b}} for b in blocked_by]}}


def verify(db, key: str, outcome: str = "merged", verdict: str = "PASS") -> None:
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,details)"
           " VALUES (%s,%s,%s,'verify','codex-01','codex','node1',1,'finished',%s,%s)",
           (str(uuid.uuid4()), key, key.split("-")[0], outcome, json.dumps({"verdict": verdict})))


def sync(ctx, issues: list[dict], full: bool = False) -> None:
    ctx.jira.versions_data["TESTWP"] = [VERSION]
    ctx.jira.search_data = issues
    jira_sync(ctx, full=full)


def version(ctx) -> dict:
    [v] = [v for v in release_readiness.project_versions(ctx.db, "TESTWP") if v["version_id"] == "100"]
    return v


def causes(v: dict) -> list[tuple]:
    return [(r["cause"], r["ticket"], r.get("status") or r.get("bug")) for r in v["reasons"]]


# --- AC1: 10 Done + 2 added mid-way --------------------------------------------------------------------------
def test_version_waits_for_tickets_added_mid_way_until_all_twelve_are_done(ctx):
    keys = [f"TESTWP-{i}" for i in range(1, 11)]
    sync(ctx, [issue(k) for k in keys], full=True)
    for k in keys:
        verify(ctx.db, k)
    assert version(ctx)["ready"] is True

    sync(ctx, [issue("TESTWP-11", "To Do"), issue("TESTWP-12", "To Do")])
    v = version(ctx)
    assert (v["ready"], v["ticket_count"], v["done_count"]) == (False, 12, 10)
    assert causes(v) == [("not_done", "TESTWP-11", "To Do"), ("not_done", "TESTWP-12", "To Do")]

    sync(ctx, [issue("TESTWP-11"), issue("TESTWP-12")])
    verify(ctx.db, "TESTWP-11")
    verify(ctx.db, "TESTWP-12")
    v = version(ctx)
    assert (v["ready"], v["ticket_count"], v["done_count"], v["reasons"]) == (True, 12, 12, [])


# --- AC2: rework, release-qa Bug, not verified ----------------------------------------------------------------
def test_done_ticket_in_rework_keeps_the_version_not_ready(ctx):
    sync(ctx, [issue("TESTWP-1"), issue("TESTWP-2", labels=["rework"])], full=True)
    verify(ctx.db, "TESTWP-1")
    verify(ctx.db, "TESTWP-2")
    assert causes(version(ctx)) == [("rework", "TESTWP-2", None)]


def test_latest_verify_in_rework_counts_as_rework(ctx):
    sync(ctx, [issue("TESTWP-1")], full=True)
    verify(ctx.db, "TESTWP-1", outcome="rework", verdict="FAIL")
    v = version(ctx)
    assert v["ready"] is False and causes(v) == [("rework", "TESTWP-1", None)]


def test_open_release_qa_bug_blocks_a_done_ticket_and_names_the_bug(ctx):
    sync(ctx, [issue("TESTWP-1", blocked_by=["TESTWP-50", "TESTWP-51", "TESTWP-52"]),
               issue("TESTWP-50", "To Do", versions=(), type_="Bug", labels=["release-qa"]),
               issue("TESTWP-51", "To Do", versions=(), type_="Bug"),  # no release-qa label: not a release blocker
               issue("TESTWP-52", "Done", versions=(), type_="Bug", labels=["release-qa"])], full=True)
    verify(ctx.db, "TESTWP-1")
    v = version(ctx)
    assert v["ready"] is False and causes(v) == [("blocked_by_bug", "TESTWP-1", "TESTWP-50")]

    sync(ctx, [issue("TESTWP-50", "Done", versions=(), type_="Bug", labels=["release-qa"])])
    assert version(ctx)["ready"] is True


def test_done_ticket_without_a_passing_verify_is_not_ready(ctx):
    sync(ctx, [issue("TESTWP-1"), issue("TESTWP-2")], full=True)
    verify(ctx.db, "TESTWP-1")
    assert causes(version(ctx)) == [("not_verified", "TESTWP-2", None)]
    verify(ctx.db, "TESTWP-2", outcome="failed", verdict="")
    assert causes(version(ctx)) == [("not_verified", "TESTWP-2", None)]
    verify(ctx.db, "TESTWP-2")
    assert version(ctx)["ready"] is True


# --- AC3: add and remove tickets ----------------------------------------------------------------------------
def test_adding_an_open_ticket_unreadies_and_removing_the_only_blocker_readies_again(ctx):
    sync(ctx, [issue("TESTWP-1"), issue("TESTWP-2")], full=True)
    verify(ctx.db, "TESTWP-1")
    verify(ctx.db, "TESTWP-2")
    assert version(ctx)["ready"] is True

    sync(ctx, [issue("TESTWP-3", "In Progress")])
    v = version(ctx)
    assert v["ready"] is False and causes(v) == [("not_done", "TESTWP-3", "In Progress")]

    sync(ctx, [issue("TESTWP-3", "In Progress", versions=())])  # PO removes the Fix version in Jira
    v = version(ctx)
    assert (v["ready"], v["ticket_count"], v["tickets"]) == (True, 2, ["TESTWP-1", "TESTWP-2"])


# --- AC4: empty version -------------------------------------------------------------------------------------
def test_empty_version_is_never_ready_and_says_so(ctx):
    sync(ctx, [], full=True)
    v = version(ctx)
    assert (v["ready"], v["ticket_count"]) == (False, 0)
    assert v["reasons"] == [{"cause": "empty", "ticket": None, "summary": ""}]
    assert release_readiness.evaluate([], {}, {})["ready"] is False


def test_released_versions_are_not_listed(ctx):
    ctx.jira.versions_data["TESTWP"] = [{**VERSION, "released": True}]
    ctx.jira.search_data = []
    jira_sync(ctx, full=True)
    assert release_readiness.project_versions(ctx.db, "TESTWP") == []


# --- AC5: reasons in Command Center (/v1/state) and Business --------------------------------------------------
def test_reasons_reach_the_state_feed_and_the_business_api(client, ctx):
    sync(ctx, [issue("TESTWP-1", "In Verify"), issue("TESTWP-2", labels=["rework"]), issue("TESTWP-3"),
               issue("TESTWP-4", blocked_by=["TESTWP-9"]),
               issue("TESTWP-9", "To Do", versions=(), type_="Bug", labels=["release-qa"])], full=True)
    verify(ctx.db, "TESTWP-4")
    expected = [("not_done", "TESTWP-1", "In Verify"), ("rework", "TESTWP-2", None),
                ("not_verified", "TESTWP-3", None), ("blocked_by_bug", "TESTWP-4", "TESTWP-9")]

    [cc] = [v for v in client.get("/v1/state").json()["release_readiness"] if v["version_id"] == "100"]
    assert cc["project"] == "TESTWP" and cc["version"] == "1.0" and cc["ready"] is False
    assert causes(cc) == expected

    biz = client.get("/v1/business/TESTWP/release-readiness")
    assert biz.status_code == 200
    [b] = biz.json()["versions"]
    assert b == cc
    assert client.get("/v1/business/UNKNOWN/release-readiness").status_code == 404


READINESS = [
    {"project": "TESTFLOW", "version_id": "100", "version": "1.0", "ready": False, "ticket_count": 4, "done_count": 3,
     "tickets": ["TESTFLOW-11", "TESTFLOW-12", "TESTFLOW-13", "TESTFLOW-14"],
     "reasons": [{"cause": "not_done", "ticket": "TESTFLOW-11", "summary": "", "status": "In Verify"},
                 {"cause": "rework", "ticket": "TESTFLOW-12", "summary": ""},
                 {"cause": "not_verified", "ticket": "TESTFLOW-13", "summary": ""},
                 {"cause": "blocked_by_bug", "ticket": "TESTFLOW-14", "summary": "", "bug": "TESTFLOW-90"}]},
    {"project": "TESTFLOW", "version_id": "200", "version": "2.0", "ready": False, "ticket_count": 0, "done_count": 0,
     "tickets": [], "reasons": [{"cause": "empty", "ticket": None, "summary": ""}]},
    {"project": "OTHER", "version_id": "300", "version": "3.0", "ready": True, "ticket_count": 1, "done_count": 1,
     "tickets": ["OTHER-1"], "reasons": []},
]


def test_business_screen_lists_each_reason_with_ticket_and_cause():
    out = business("renderBBody(); console.log(JSON.stringify({biz: el('#bbody').innerHTML}));",
                   state={**STATE, "release_progress": READINESS})["biz"]
    assert 'id="b-ready"' in out and "Tiến độ release" in out
    assert "TESTFLOW-11: chưa Done (đang ở In Verify)" in out
    assert "TESTFLOW-12: đang rework" in out
    assert "TESTFLOW-13: đã Done nhưng chưa qua verify" in out
    assert "TESTFLOW-14: bị Bug release-qa TESTFLOW-90 chặn" in out
    assert "Version rỗng: chưa có ticket nào" in out
    assert "3 / 4 Done" in out and 'data-state="notReady"' in out
    assert "3.0" not in out  # other project's version stays on its own tab


def test_command_center_panel_lists_every_project_version_with_reasons():
    out = json.loads(run_script(DOM + f"""
console.log(JSON.stringify({{cc: readinessPanel({json.dumps(READINESS)}, 'en', 'p-ready')}}));
"""))["cc"]
    assert 'id="p-ready"' in out and "Release readiness" in out
    assert "TESTFLOW-11: not Done (status In Verify)" in out
    assert "TESTFLOW-12: in rework" in out
    assert "TESTFLOW-13: Done but not verified" in out
    assert "TESTFLOW-14: blocked by release-qa Bug TESTFLOW-90" in out
    assert "The version is empty" in out
    assert 'data-version="300" data-ready="yes"' in out


def test_command_center_renders_the_readiness_panel():
    from test_console import PAGE
    page = PAGE.read_text()
    render_cc = page[page.index("function renderCC()"):page.index("function renderNodeJoin(")]
    assert "progressPanel(d.release_progress || [], 'en', 'p-ready', true)" in render_cc
