"""Business view (/business): epic progress and "Cần bạn trả lời" from /v1/state, the change request form, and the
impact assessment of an intake refiner. Jira is faked; the console script runs in Node.js."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from conftest import add_ticket
from test_console import STUBS, html, run_script
from test_intake import Api, FakeAdapter, FakeGit, iclient  # noqa: F401  (iclient is a fixture)
from aiflow.v4.control.api import create_app
from aiflow.v4.control.ui import mount_ui
from aiflow.v4.worker import impact
from aiflow.v4.worker.dev import Claimed

ASSESSMENT = """I read the board.
SIZE: M
EPICS: TESTWP-1
TICKETS: TESTWP-3, TESTWP-4
RISKS:
- the checkout page is cached for an hour
CONFLICTS:
- TESTWP-3 rewrites the same template
IMPACT: DONE
"""


def epic(db, key, summary="Epic", status="In Progress"):
    add_ticket(db, key, status=status, labels=())
    db.run("UPDATE tickets SET type='Epic', summary=%s WHERE key=%s", (summary, key))


def child(db, key, parent, status="To Do", labels=()):
    add_ticket(db, key, status=status, labels=labels)
    db.run("UPDATE tickets SET type='Story', parent_key=%s WHERE key=%s", (parent, key))


@pytest.fixture
def web(ctx):
    """The control API as the control-api CLI serves it: API plus console pages, without a node token."""
    app = create_app(ctx)
    mount_ui(app, ctx)
    app.state.sync_accounts()
    return TestClient(app)


def post_cr(client, project="TESTWP", **body):
    return client.post(f"/v1/business/{project}/change-requests", json=body)


# --- AC1: the page, with the console's access ----------------------------------------------------------------
def test_business_page_is_the_console_with_the_same_access(web):
    page = web.get("/business")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.text == web.get("/cc").text == web.get("/delivery").text
    assert 'data-view="business"' in page.text and 'id="view-business"' in page.text
    # no login, role or node token: the same as /cc, /delivery and /v1/state
    assert "authorization" not in {k.lower() for k in web.headers}
    assert web.get("/v1/business/TESTWP/change-requests").status_code == 200
    assert web.get("/v1/business/NOPE/change-requests").status_code == 404


def test_business_route_is_routed_by_traefik():
    from pathlib import Path
    compose = (Path(__file__).resolve().parents[2] / "deploy" / "v4" / "control" / "compose.yaml").read_text()
    assert "PathPrefix(`/business`)" in compose


# --- AC2 and AC3: one project, epic progress as in Delivery, Needs owner ------------------------------------
STATE = {
    "projects": [{"key": "TESTFLOW", "repo": "jooservices/testflow"}, {"key": "OTHER", "repo": "jooservices/other"}],
    "tickets": [
        {"key": "TESTFLOW-1", "project": "TESTFLOW", "type": "Epic", "summary": "Checkout", "status": "In Progress", "labels": []},
        {"key": "TESTFLOW-2", "project": "TESTFLOW", "type": "Epic", "summary": "Search", "status": "To Do", "labels": []},
        {"key": "TESTFLOW-3", "project": "TESTFLOW", "type": "Story", "summary": "Pay", "status": "Done", "labels": [], "parent_key": "TESTFLOW-1"},
        {"key": "TESTFLOW-4", "project": "TESTFLOW", "type": "Story", "summary": "Refund", "status": "Needs owner", "labels": [], "parent_key": "TESTFLOW-1"},
        {"key": "TESTFLOW-5", "project": "TESTFLOW", "type": "Sub-task", "summary": "Refund mail", "status": "In Verify", "labels": [], "parent_key": "TESTFLOW-4"},
        {"key": "TESTFLOW-6", "project": "TESTFLOW", "type": "Bug", "summary": "Typo", "status": "Needs owner", "labels": ["owner-escalated"], "parent_key": "TESTFLOW-2"},
        {"key": "TESTFLOW-7", "project": "TESTFLOW", "type": "Story", "summary": "Filters", "status": "Human review", "labels": [], "parent_key": "TESTFLOW-2"},
        {"key": "TESTFLOW-8", "project": "TESTFLOW", "type": "Story", "summary": "Loose", "status": "Intake", "labels": []},
        {"key": "OTHER-1", "project": "OTHER", "type": "Epic", "summary": "Other epic", "status": "To Do", "labels": []},
        {"key": "OTHER-2", "project": "OTHER", "type": "Story", "summary": "Other wait", "status": "Needs owner", "labels": [], "parent_key": "OTHER-1"},
    ],
    "recent_runs": [], "active_runs": [], "incidents": [], "events": [], "totals": {}, "versions": {},
}
DOM = """
var els = {};
function el(id) {
  if (!els[id]) els[id] = {innerHTML: '', textContent: '', hidden: false, value: '',
    setAttribute: function () {}, getAttribute: function () { return null; }, removeAttribute: function () {},
    classList: {add: function () {}, remove: function () {}}, scrollIntoView: function () {}};
  return els[id];
}
document.querySelector = function (s) { return el(s); };
document.getElementById = function (id) { return el('#' + id); };
"""


def business(driver: str, prelude: str = STUBS, state: dict = STATE, project: str = "TESTFLOW") -> dict:
    out = run_script(DOM + f"""
S.data = {json.dumps(state)}; S.project = {json.dumps(project)}; S.view = 'business';
S.biz = {{project: {json.dumps(project)}, jira_site: 'https://jira.example', change_requests: []}};
""" + driver, prelude)
    return json.loads(out)


def epic_rows(html: str) -> list[tuple[str, str]]:
    return re.findall(r'data-key="([A-Z]+-\d+)">.*?<div class="e-s">(.*?)</div>.*?(\d+)%</span></button>', html)


def test_business_shows_only_the_chosen_project_with_the_delivery_epic_numbers():
    out = business("""
renderBusiness(); var biz = el('#bbody').innerHTML, head = el('#bhead').innerHTML;
renderDBody(); var delivery = el('#dbody').innerHTML;
console.log(JSON.stringify({biz: biz, head: head, delivery: delivery, rows: epicProgress(projTickets())}));
""")
    assert 'data-act="project" data-p="TESTFLOW" aria-pressed="true"' in out["head"]
    assert 'data-p="OTHER" aria-pressed="false"' in out["head"]
    assert "OTHER-" not in out["biz"]
    rows = epic_rows(out["biz"])
    assert rows == epic_rows(out["delivery"])  # same function, same data, same numbers
    assert rows == [("TESTFLOW-1", "3 items · 1 done · In Progress", "33"), ("TESTFLOW-2", "2 items · 0 done · To Do", "0")]
    counts = {r["epic"]["key"]: r["counts"] for r in out["rows"]}
    assert counts["TESTFLOW-1"] == {"done": 1, "verify": 1, "progress": 0, "wait": 1, "todo": 0}
    assert counts["TESTFLOW-2"] == {"done": 0, "verify": 1, "progress": 0, "wait": 1, "todo": 0}


def test_switching_project_shows_the_other_projects_data():
    out = business("renderBusiness(); console.log(JSON.stringify({biz: el('#bbody').innerHTML}));", project="OTHER")
    assert "OTHER-1" in out["biz"] and "OTHER-2" in out["biz"] and "TESTFLOW-" not in out["biz"]


def test_needs_your_answer_lists_exactly_the_needs_owner_tickets_of_the_project():
    out = business("renderBBody(); console.log(JSON.stringify({biz: el('#bbody').innerHTML}));")
    html = out["biz"]
    assert "Cần bạn trả lời" in html
    waiting = re.findall(r'data-wait="([A-Z]+-\d+)"', html)
    assert waiting == ["TESTFLOW-4", "TESTFLOW-6"]  # an intake/dev question and a triage escalation; nothing else
    item = html.split('data-wait="TESTFLOW-6"', 1)[1].split("</a></div>", 1)[0]
    assert "Typo" in item and "Epic: TESTFLOW-2" in item and "Triage escalate" in item
    assert 'href="https://jira.example/browse/TESTFLOW-6"' in item
    first = html.split('data-wait="TESTFLOW-4"', 1)[1].split("</a></div>", 1)[0]
    assert "Refund" in first and "Epic: TESTFLOW-1" in first and 'href="https://jira.example/browse/TESTFLOW-4"' in first


# --- AC4: the form ---------------------------------------------------------------------------------------
def test_form_has_title_description_and_the_projects_epics():
    out = business("renderBForm(); console.log(JSON.stringify({form: el('#bform').innerHTML}));")
    form = out["form"]
    assert 'id="cr-title"' in form and 'id="cr-desc"' in form and 'id="cr-epic"' in form
    assert re.findall(r'<option value="([^"]*)"', form) == ["", "TESTFLOW-1", "TESTFLOW-2"]


def test_form_does_not_send_without_title_or_description():
    prelude = STUBS + "var sent = []; fetch = function (u, o) { sent.push(u); return new Promise(function () {}); };"
    out = business("""
renderBForm();
S.cr.v = {title: '  ', description: 'Make refunds faster', epic: ''};
var first = submitCR(), e1 = el('#bform').innerHTML;
S.cr.v = {title: 'Faster refunds', description: '', epic: ''};
var second = submitCR(), e2 = el('#bform').innerHTML;
console.log(JSON.stringify({first: first, second: second, e1: e1, e2: e2, sent: sent}));
""", prelude)
    assert out["first"] is False and out["second"] is False and out["sent"] == []
    assert "Thiếu tiêu đề" in out["e1"] and 'role="alert"' in out["e1"]
    assert "Thiếu mô tả" in out["e2"]


def test_api_refuses_incomplete_forms_and_foreign_epics(web, ctx):
    epic(ctx.db, "TESTWP-1")
    add_ticket(ctx.db, "TESTWP-5")  # a Story, not an epic
    add_ticket(ctx.db, "OTHER-1", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='OTHER-1'")
    for body, error in (({"title": "", "description": "d", "epic": "TESTWP-1"}, "title"),
                        ({"title": "t", "description": "  ", "epic": "TESTWP-1"}, "description"),
                        ({"title": "t", "description": "d", "epic": "OTHER-1"}, "not an epic of project TESTWP"),
                        ({"title": "t", "description": "d", "epic": "TESTWP-5"}, "not an epic of project TESTWP")):
        r = post_cr(web, **body)
        assert r.status_code == 400 and error in r.json()["detail"], (body, r.text)
    assert not getattr(ctx.jira, "created", [])
    assert not ctx.db.all("SELECT 1 FROM write_audit WHERE action='create'")


# --- AC5: the Story in Jira --------------------------------------------------------------------------------
def test_valid_change_request_creates_one_backlog_story_under_the_epic(web, ctx):
    ctx.settings.jira_site = "https://jira.example"
    epic(ctx.db, "TESTWP-1")
    r = post_cr(web, title="Faster refunds", description="Refunds should take one day.", epic="TESTWP-1")
    assert r.status_code == 200, r.text
    assert r.json() == {"key": "TESTWP-900", "status": "Backlog", "epic": "TESTWP-1",
                        "url": "https://jira.example/browse/TESTWP-900"}
    assert ctx.jira.created == [{"key": "TESTWP-900", "project": "TESTWP", "summary": "Faster refunds",
                                 "description": "Refunds should take one day.", "issuetype": "Story",
                                 "labels": ["change-request"], "parent": "TESTWP-1"}]
    assert ctx.jira.status_of["TESTWP-900"] == "Backlog"
    row = ctx.db.one("SELECT status, type, labels, parent_key FROM tickets WHERE key='TESTWP-900'")
    assert row == {"status": "Backlog", "type": "Story", "labels": ["change-request"], "parent_key": "TESTWP-1"}
    audit = ctx.db.all("SELECT target, action, actor, accepted, payload FROM write_audit WHERE ticket='TESTWP-900'")
    assert len(audit) == 1 and audit[0]["target"] == "jira" and audit[0]["action"] == "create" and audit[0]["accepted"]
    assert audit[0]["actor"] == "business-console" and audit[0]["payload"]["labels"] == ["change-request"]


def test_a_project_whose_new_issues_start_elsewhere_still_gets_backlog(web, ctx):
    ctx.jira.initial_status = "To Do"
    r = post_cr(web, title="Dark mode", description="Add a dark theme.")
    assert r.status_code == 200 and r.json()["status"] == "Backlog" and r.json()["epic"] is None
    assert ctx.jira.status_of[r.json()["key"]] == "Backlog" and ctx.jira.created[0]["parent"] == ""


def test_the_screen_shows_the_new_key_and_link():
    prelude = STUBS + """
var sent = [];
fetch = function (u, o) { sent.push({url: u, method: (o || {}).method, body: (o || {}).body});
  var body = o && o.method === 'POST' ? {key: 'TESTFLOW-900', status: 'Backlog', epic: 'TESTFLOW-1', url: 'https://jira.example/browse/TESTFLOW-900'}
    : {project: 'TESTFLOW', change_requests: []};
  return Promise.resolve({ok: true, status: 200, json: function () { return Promise.resolve(body); }}); };
"""
    out = business("""
renderBForm();
S.cr.v = {title: 'Faster refunds', description: 'One day', epic: 'TESTFLOW-1'};
submitCR();
setTimeout(function () { console.log(JSON.stringify({form: el('#bform').innerHTML, sent: sent})); }, 20);
""", prelude)
    post = out["sent"][0]
    assert post["url"] == "/v1/business/TESTFLOW/change-requests" and post["method"] == "POST"
    assert json.loads(post["body"]) == {"title": "Faster refunds", "description": "One day", "epic": "TESTFLOW-1"}
    assert 'href="https://jira.example/browse/TESTFLOW-900"' in out["form"] and "TESTFLOW-900" in out["form"]


# --- AC6, AC7 and AC8: the impact assessment ----------------------------------------------------------------
def assess(iclient, ctx, tmp_path, monkeypatch, key, answer=ASSESSMENT):
    adapter = FakeAdapter(answer)
    monkeypatch.setattr(impact, "Git", FakeGit)
    monkeypatch.setattr(impact, "get_adapter", lambda provider: adapter)
    r = iclient.post("/v1/work/claim", json={"ticket": key, "account": "grok-01", "step": "impact"})
    assert r.status_code == 200, r.text
    r = r.json()
    c = Claimed(run_id=r["run_id"], fence=r["fence"], run_token=r["run_token"], account=r["account"],
                provider=r["provider"], model=r["model"], effort=r["effort"], ticket=r["ticket"], project=r["project"],
                identity=tuple(r["identity"]), step="impact", project_tickets=r.get("project_tickets") or [])
    res = impact.run_impact(Api(iclient), c, str(tmp_path), "http://control", 3600, [])
    iclient.post(f"/v1/runs/{c.run_id}/result", json=res)
    iclient.post(f"/v1/runs/{c.run_id}/release", json={"fence": c.fence})
    return res, adapter, r


def candidates(client, account="grok-01"):
    return [(t["key"], t["step"]) for t in client.get("/v1/work/candidates", params={"account": account}).json()["tickets"]]


def test_change_request_is_assessed_once_and_the_comment_has_every_part(iclient, ctx, tmp_path, monkeypatch):
    epic(ctx.db, "TESTWP-1", "Checkout")
    child(ctx.db, "TESTWP-3", "TESTWP-1", status="In Progress")
    child(ctx.db, "TESTWP-4", "TESTWP-1", status="Done")
    key = post_cr(iclient, title="Faster refunds", description="Refunds in one day.", epic="TESTWP-1").json()["key"]
    add_ticket(ctx.db, "TESTWP-50", status="Backlog", labels=())  # an ordinary Backlog ticket is not assessed

    assert candidates(iclient) == [(key, "impact")]
    assert (key, "impact") not in candidates(iclient, "claude-01")  # intake refiners only
    res, adapter, claimed = assess(iclient, ctx, tmp_path, monkeypatch, key)

    assert res["outcome"] == "assessed"
    prompt = adapter.prompts[0]
    assert "Refunds in one day." in prompt and "TESTWP-1" in prompt and "TESTWP-3 · Story · In Progress" in prompt
    assert "TESTWP-4 ·" not in prompt  # done work is not on the board
    comments = [t for k, t in ctx.jira.comments if k == key]
    assert len(comments) == 1
    text = comments[0]
    assert "Size: M" in text and "Affected epics: TESTWP-1" in text and "Affected tickets: TESTWP-3, TESTWP-4" in text
    assert "the checkout page is cached for an hour" in text and "TESTWP-3 rewrites the same template" in text
    stored = ctx.db.one("SELECT details FROM runs WHERE ticket_key=%s AND step='impact'", (key,))["details"]["impact"]
    assert stored == {"size": "M", "epics": ["TESTWP-1"], "tickets": ["TESTWP-3", "TESTWP-4"],
                      "risks": ["the checkout page is cached for an hour"],
                      "conflicts": ["TESTWP-3 rewrites the same template"]}
    assert candidates(iclient) == []  # one assessment per change request


def test_change_requests_show_assessing_then_the_assessment(iclient, ctx, tmp_path, monkeypatch):
    epic(ctx.db, "TESTWP-1")
    key = post_cr(iclient, title="Faster refunds", description="Refunds in one day.", epic="TESTWP-1").json()["key"]
    add_ticket(ctx.db, "TESTWP-60", status="To Do", labels=())  # not a change request
    listed = iclient.get("/v1/business/TESTWP/change-requests").json()
    assert [c["key"] for c in listed["change_requests"]] == [key]
    assert listed["change_requests"][0]["assessment"] == "assessing" and listed["change_requests"][0]["impact"] is None
    assert listed["epics"] == [{"key": "TESTWP-1", "summary": "Epic", "status": "In Progress"}]

    assess(iclient, ctx, tmp_path, monkeypatch, key)
    cr = iclient.get("/v1/business/TESTWP/change-requests").json()["change_requests"][0]
    assert cr["assessment"] == "done" and cr["impact"]["size"] == "M" and cr["epic"] == "TESTWP-1"
    assert cr["impact"]["conflicts"] == ["TESTWP-3 rewrites the same template"] and cr["status"] == "Backlog"


def test_screen_renders_assessing_and_assessed_change_requests():
    crs = [{"key": "TESTFLOW-901", "summary": "Dark mode", "status": "Backlog", "epic": None, "url": "u1",
            "assessment": "assessing", "impact": None},
           {"key": "TESTFLOW-900", "summary": "Faster refunds", "status": "Backlog", "epic": "TESTFLOW-1", "url": "u0",
            "assessment": "done", "impact": {"size": "L", "epics": ["TESTFLOW-1"], "tickets": ["TESTFLOW-3"],
                                             "risks": ["cache"], "conflicts": ["TESTFLOW-3 same template"]}}]
    out = business(f"""
S.biz.change_requests = {json.dumps(crs)};
renderBCR(); console.log(JSON.stringify({{crs: el('#bcr').innerHTML}}));
""")
    pending, done = out["crs"].split('data-cr="TESTFLOW-901"', 1)[1].split('data-cr="TESTFLOW-900"', 1)
    assert "Đang đánh giá" in pending and "Độ lớn" not in pending
    assert "Độ lớn L" in done and "TESTFLOW-1" in done and "TESTFLOW-3" in done
    assert "<li>cache</li>" in done and "<li>TESTFLOW-3 same template</li>" in done and "Đang đánh giá" not in done


def test_assessment_never_moves_or_rewrites_the_ticket(iclient, ctx, tmp_path, monkeypatch):
    key = post_cr(iclient, title="Dark mode", description="Add a dark theme.").json()["key"]
    transitions_before = dict(ctx.jira.status_of)
    _res, _adapter, r = assess(iclient, ctx, tmp_path, monkeypatch, key)
    assert ctx.jira.status_of == transitions_before and ctx.jira.status_of[key] == "Backlog"
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (key,))["status"] == "Backlog"
    assert not getattr(ctx.jira, "descriptions", []) and not ctx.jira.labels and not ctx.jira.fields
    actions = [a["action"] for a in ctx.db.all("SELECT action FROM write_audit WHERE ticket=%s AND accepted ORDER BY id", (key,))]
    assert actions == ["create", "token", "comment"]

    # even a misbehaving impact worker cannot move, relabel or rewrite the ticket
    add_ticket(ctx.db, "TESTWP-70", status="Backlog", labels=("change-request",))
    run = iclient.post("/v1/work/claim", json={"ticket": "TESTWP-70", "account": "grok-01", "step": "impact"}).json()
    fence = {"run_id": run["run_id"], "fence": run["fence"]}
    for target in ("Intake", "To Do"):
        assert iclient.post("/v1/jira/TESTWP-70/transition", json={**fence, "to": target}).status_code == 403
    assert iclient.post("/v1/jira/TESTWP-70/fields", json={**fence, "add_labels": ["aiflow-ready"]}).status_code == 403
    assert iclient.post("/v1/jira/TESTWP-70/description", json={**fence, "body": "x"}).status_code == 403
    assert "TESTWP-70" not in ctx.jira.status_of
    hook = {"tool_name": "Write", "tool_input": {"file_path": "/srv/aiflow/work/x/a.txt"}, "cwd": "/srv/aiflow/work/x"}
    out = iclient.post("/v1/policy/decide", json=hook, headers={"Authorization": f"Bearer {run['run_token']}"}).json()
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_business_screen_has_no_status_change_action():
    out = business("""
S.biz.change_requests = [{key: 'TESTFLOW-900', summary: 'x', status: 'Backlog', epic: null, url: 'u', impact: null}];
renderBusiness();
console.log(JSON.stringify({html: el('#bhead').innerHTML + el('#bbody').innerHTML + el('#bform').innerHTML + el('#bcr').innerHTML}));
""")
    acts = set(re.findall(r'data-act="([^"]+)"', out["html"]))
    assert acts <= {"project", "open-ticket"}  # choose a project, open the read-only ticket drawer
    assert "transition" not in out["html"].lower()
    source = re.search(r"/\* ---------- Business:(.*?)/\* ---------- Analytics", html(), re.S).group(1)
    assert "transition" not in source.split("*/", 1)[1] and "/v1/jira" not in source


@pytest.mark.parametrize(("answer", "usable"), [
    (ASSESSMENT, True),
    ("SIZE: S\nEPICS: none\nTICKETS: none\nRISKS:\n- none\nCONFLICTS:\n- none\nIMPACT: DONE\n", True),
    (ASSESSMENT.replace("IMPACT: DONE", ""), False),
    (ASSESSMENT.replace("SIZE: M", "SIZE: XL"), False),
    (ASSESSMENT.replace("CONFLICTS:\n- TESTWP-3 rewrites the same template\n", ""), False),
])
def test_parse_assessment(answer, usable):
    parsed = impact.parse(answer)
    assert (parsed is not None) is usable
    if parsed and "none" in answer.split("EPICS:", 1)[1].split("\n", 1)[0]:
        assert parsed == {"size": "S", "epics": [], "tickets": [], "risks": [], "conflicts": []}
        assert "Affected epics: none" in impact.render(parsed) and "- none" in impact.render(parsed)


def test_unusable_assessment_posts_nothing_and_retries_later(iclient, ctx, tmp_path, monkeypatch):
    key = post_cr(iclient, title="Dark mode", description="Add a dark theme.").json()["key"]
    res, _, _ = assess(iclient, ctx, tmp_path, monkeypatch, key, answer="It is big.")
    assert res["outcome"] == "failed" and not ctx.jira.comments
    assert candidates(iclient) == []  # waits before the next try
    listed = iclient.get("/v1/business/TESTWP/change-requests").json()["change_requests"]
    assert listed[0]["assessment"] == "assessing"
