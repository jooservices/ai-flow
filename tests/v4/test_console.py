"""Console page: node registration details, full-width layout, global footer, no new libraries."""
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiflow.v4 import VERSION
from aiflow.v4.core.settings import Settings

PAGE = Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "ui" / "console.html"
BOOT = "/* ---------- boot ---------- */"
STUBS = """
var document = {addEventListener: function () {}, documentElement: {setAttribute: function () {}},
  getElementById: function () { return null; }, querySelector: function () { return null; },
  querySelectorAll: function () { return []; }};
var window = {matchMedia: function () { return {matches: false}; }, addEventListener: function () {}};
var location = {pathname: '/cc'};
var localStorage = {getItem: function () { return null; }, setItem: function () {}};
var fetch = function () {};
var setInterval = function () {};
"""


def run_script(driver: str, prelude: str = STUBS) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to run the console script")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    result = subprocess.run([node_js, "-"], input=prelude + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def render_drawer(node: dict, console_user: dict | None = None, operations: list[dict] | None = None) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the console drawer")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    driver = f"""
var node = {json.dumps(node)};
S.data = {{nodes: [node], accounts: [], active_runs: [], recent_runs: [], projects: [], tickets: [],
          incidents: [], events: [], totals: {{}}, versions: {{}}}};
S.consoleUser = {json.dumps(console_user)};
S.nodeOperations = {{[node.name]: {json.dumps(operations or [])}}};
S.fetchedAt = Date.now();
console.log(drawerNode(node.name));
"""
    result = subprocess.run([node_js, "-"], input=STUBS + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def render_sa(state: dict) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the SA dashboard")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    driver = f"""
var saView = {{innerHTML: ''}};
document.querySelector = function (selector) {{ return selector === '#view-sa' ? saView : null; }};
S.data = {json.dumps(state)};
renderSA();
console.log(saView.innerHTML);
"""
    result = subprocess.run([node_js, "-"], input=STUBS + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def html() -> str:
    return PAGE.read_text()


def css() -> str:
    return re.search(r"<style>(.*?)</style>", html(), re.S).group(1)


def test_node_drawer_shows_registration_details_online_and_last_seen_offline():
    now = datetime.now(timezone.utc)
    node = {
        "name": "node3", "ip": "192.0.2.55", "role": "worker", "status": "online",
        "slots_total": 2, "slots_used": 0, "release": "v1",
        "metrics": {"hostname": "worker-3", "cpus": 8}, "tools": {},
        "heartbeat_age_s": 12, "joined_at": (now - timedelta(days=1, hours=2)).isoformat(),
        "last_heartbeat": (now - timedelta(seconds=12)).isoformat(),
        "purpose": "", "notes": "", "capabilities": [], "projects": ["TESTWP"],
    }

    online = render_drawer(node)
    for label in ("IP", "Joined", "Last heartbeat", "Hostname"):
        assert f'<span class="fl">{label}</span>' in online
    assert "192.0.2.55" in online and "worker-3" in online
    assert re.search(r"\(\d+ s ago\)", online)
    assert "last seen " not in online
    assert '<span class="fl">Joined</span><span class="fv">—</span>' not in online
    assert '<span class="fl">Last heartbeat</span><span class="fv">never</span>' not in online

    node.update(status="offline", heartbeat_age_s=95,
                last_heartbeat=(now - timedelta(seconds=95)).isoformat())
    offline = render_drawer(node)
    assert "Offline" in offline
    assert re.search(r"last seen (?:\d{1,2}/\d{1,2} )?\d{2}:\d{2} \(1 min ago\)", offline)
    assert "The node stopped sending heartbeats." in offline


def test_node_drawer_lists_missing_project_tools_in_red():
    now = datetime.now(timezone.utc)
    node = {
        "name": "node3", "ip": "192.0.2.55", "role": "worker", "status": "online",
        "slots_total": 2, "slots_used": 0, "release": "v1",
        "metrics": {"hostname": "worker-3", "cpus": 8},
        "tools": {"php": "PHP 8.3.0", "composer": "missing"},
        "missing_tools": ["composer", "wp"],
        "heartbeat_age_s": 12, "joined_at": (now - timedelta(days=1, hours=2)).isoformat(),
        "last_heartbeat": (now - timedelta(seconds=12)).isoformat(),
        "purpose": "", "notes": "", "capabilities": [], "projects": ["TESTWP"],
    }

    drawer = render_drawer(node)
    assert '<span class="vk">composer</span><span class="vv bad">missing</span>' in drawer
    assert '<span class="vk">wp</span><span class="vv bad">missing</span>' in drawer
    assert "Missing for this node's project stacks: composer, wp." in drawer


def test_node_drawer_exposes_owner_install_repair_join_and_lead_audit_view():
    node = {"name": "node3", "ip": "192.0.2.55", "role": "worker", "status": "online",
            "slots_total": 2, "slots_used": 0, "release": "v1", "metrics": {}, "tools": {"codex": "missing"},
            "missing_tools": ["codex"], "has_agent": True, "required_tools": ["codex"],
            "desired_services": [{"name": "worker-api", "unit": "worker-api.service",
                                  "healthcheck_configured": True}], "projects": [], "heartbeat_age_s": 12}
    owner = render_drawer(node, {"authenticated": True, "user": "owner", "role": "owner"})
    assert 'data-act="node-install"' in owner and 'data-act="node-repair"' in owner
    assert "worker-api.service" in owner and "health check configured" in owner
    lead = render_drawer(node, {"authenticated": True, "user": "lead", "role": "lead"})
    assert 'data-act="node-install"' not in lead and 'data-act="node-repair"' not in lead
    assert "Only an authenticated owner can install or repair a node." in lead
    no_agent = dict(node, status="joined", has_agent=False)
    join = render_drawer(no_agent, {"authenticated": True, "user": "owner", "role": "owner"})
    assert 'data-act="join-command"' in join


def test_node_operation_log_escapes_service_names_from_saved_results():
    service_name = '<img src=x onerror="alert(1)">'
    node = {"name": "node3", "ip": "192.0.2.55", "status": "online", "metrics": {}, "tools": {},
            "has_agent": True, "projects": [], "heartbeat_age_s": 12}
    rendered = render_drawer(node, {"authenticated": True, "user": "lead", "role": "lead"}, [{
        "status": "completed", "action": "repair", "actor": "owner", "created_at": None,
        "result": {"services": [{"name": service_name, "healthy": True}]},
    }])
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; green" in rendered
    assert service_name not in rendered


def test_layout_uses_the_full_width_with_a_wider_gutter_on_large_screens():
    style = css()
    assert "max-width:1400px" not in style
    wrap = re.search(r"\n\.wrap\{([^}]*)\}", style).group(1)
    assert "max-width:none" in wrap and "width:100%" in wrap and "max(16px" in wrap
    wide = re.search(r"@media \(min-width:1401px\)\{ \.wrap\{([^}]*)\}", style).group(1)
    assert "max(24px" in wide


def test_small_screens_keep_the_gutter_and_never_scroll_sideways():
    style = css()
    assert re.search(r"\nhtml\{overflow-x:hidden\}", style)
    body = re.search(r"\nbody\{([^}]*)\}", style).group(1)
    assert "margin:0" in body and "overflow-x:hidden" in body
    # the 16 px gutter is the base rule, so it applies below 640 px as well
    assert "padding-left:max(16px" in re.search(r"\n\.wrap\{([^}]*)\}", style).group(1)


def test_runs_table_shows_triage_step():
    rendered = run_script("console.log(runsTable([{step: 'triage', ticket: 'TESTWP-70', account: 'codex-01', "
                          "model: 'gpt-6-luna', node: 'node1', outcome: 'answered', duration_s: 12, "
                          "tokens_in: 1, tokens_out: 2, cost_usd: 0}], 10));")
    assert "<td>triage</td>" in rendered


def test_footer_has_version_environment_control_ref_links_and_copyright():
    page = html()
    footer = re.search(r'<footer class="footer" id="footer">(.*?)</footer>', page, re.S).group(1)
    for el in ("f-version", "f-env", "f-control", "f-year"):
        assert f'id="{el}"' in footer
    assert 'href="/o2/"' in footer and ">OpenObserve<" in footer
    assert 'href="/mail/"' in footer and ">Mailpit<" in footer
    assert 'id="f-gatus"' in footer and ">Gatus<" in footer
    assert "JOOservices" in footer and "&copy;" in footer
    assert "new Date().getFullYear()" in page
    assert "renderFooter();" in page
    # version: versions.lock ai_flow ref first, then the running VERSION from /v1/state
    assert "((d.versions || {}).ai_flow || {}).ref || d.version" in page


def test_no_new_libraries_and_both_themes_still_defined():
    page = html()
    assert "<script src" not in page and "<script type=\"module\"" not in page
    assert page.count("<script>") == 1 and page.count("<style>") == 1
    assert ':root[data-theme="dark"]' in page and ':root:not([data-theme="light"])' in page
    footer_css = "\n".join(line for line in css().splitlines() if line.startswith(".footer"))
    assert "#" not in footer_css.replace("#f-", "")  # footer colours come from theme variables only


def test_console_page_and_state_feed_the_footer(ctx):
    from fastapi.testclient import TestClient
    from aiflow.v4.control.api import create_app
    from aiflow.v4.control.ui import mount_ui
    app = create_app(ctx)
    mount_ui(app, ctx)  # as the control-api CLI does
    app.state.sync_accounts()
    client = TestClient(app)
    page = client.get("/cc")
    assert page.status_code == 200 and 'id="footer"' in page.text
    assert client.get("/sa").status_code == 200
    assert 'id="view-sa"' in page.text and 'data-view="sa"' in page.text
    lead = client.get("/lead")
    assert lead.status_code == 200 and 'id="tab-lead"' in lead.text and 'id="view-lead"' in lead.text
    s = client.get("/v1/state").json()
    assert s["version"] == VERSION
    assert s["env"] and "control_repo" in s and "versions" in s
    assert s["links"] == {"o2": "/o2/", "mail": "/mail/", "gatus": "/gatus/"}


def test_sa_dashboard_renders_thresholds_incidents_expiry_blocks_and_openobserve_link():
    state = {
        "nodes": [{"name": "node1", "ip": "192.0.2.1", "status": "online", "slots_total": 4,
                   "slots_used": 3, "heartbeat_age_s": 10, "metrics": {"cpu_pct": 81, "mem_total": 100,
                   "mem_used": 50, "disk_free_pct": 20}}],
        "accounts": [{"id": "claude-01", "provider": "claude", "node": "node1", "status": "signed_out",
                      "provider_status": "signed_out", "check_failed": True, "credential_expires_at": "unknown",
                      "credential_expiry_warning": False, "quota": {}, "roles": [], "last_error": "sign in"}],
        "incidents": [{"id": 1, "severity": "warning", "kind": "account_quota", "title": "Quota low",
                       "detail": "provider cooldown", "opened_at": "2026-10-21T00:00:00+00:00"}],
        "active_runs": [], "recent_runs": [], "tickets": [], "events": [], "incidents_recent": [],
        "totals": {}, "versions": {}, "links": {"o2": "/o2/"},
        "sa": {"thresholds": {"slots_used_percent": 80, "slots_duration_minutes": 30,
                                "role_cooldown_minutes": 15, "idle_days": 7},
               "slots": {"used": 3, "total": 4, "utilization_percent": 75},
               "recommendations": [{"kind": "add_node", "subject": "fleet", "reason": "slot pressure"}],
               "provider_blocks": [{"id": "claude-01", "provider_status": "signed_out", "status": "signed_out",
                                    "last_error": "sign in"}],
               "policy_denials": [{"ts": "2026-10-21T00:00:00+00:00", "ticket": "TESTWP-3",
                                   "account": "claude-01", "reason": "denied", "command": "git push"}],
               "network_issues": [{"source": "event", "ts": "2026-10-21T00:00:00+00:00",
                                   "type": "node.heartbeat.error", "node": "node1", "reason": "network down",
                                   "data": {"retry": True}}]},
    }
    rendered = render_sa(state)
    for label in ("Capacity recommendations", "Nodes and capacity", "Account access, quota and credentials",
                  "AI blocked by policy", "AI blocked by provider", "Network errors and retries", "Incidents",
                  "git push", "TESTWP-3", "Signed out", "network down", "OpenObserve", "scheduled"):
        assert label in rendered
    page = html()
    assert "function viewOf(path)" in page
    assert run_script("console.log(['/cc', '/delivery', '/sa', '/business'].map(viewOf).join(','));") == \
        "cc,delivery,sa,business\n"
    assert "fetch('/api/default/" not in page and "fetch('/o2/" not in page


def _sa_state(**overrides):
    state = {
        "nodes": [], "accounts": [], "incidents": [], "active_runs": [], "recent_runs": [], "tickets": [],
        "events": [], "incidents_recent": [], "totals": {}, "versions": {}, "links": {"o2": "/o2/"},
        "sa": {"thresholds": {"slots_used_percent": 80, "slots_duration_minutes": 30, "role_cooldown_minutes": 15,
                              "idle_days": 7, "credential_expiry_days": 7},
               "slots": {"used": 0, "total": 0, "utilization_percent": None}, "recommendations": [],
               "provider_blocks": [], "policy_denials": [], "network_issues": []},
    }
    for key, value in overrides.items():
        if key in state["sa"]:
            state["sa"][key] = value
        else:
            state[key] = value
    return state


def test_sa_node_metrics_sent_as_strings_are_not_rendered_as_html():
    payload = "<img src=x onerror=alert(1)>"
    rendered = render_sa(_sa_state(nodes=[{
        "name": "node1", "status": "online", "slots_total": 2, "slots_used": 1, "heartbeat_age_s": 5,
        "metrics": {"cpu_pct": payload, "disk_free_pct": payload, "mem_total": payload, "mem_used": payload}}]))
    assert "<img" not in rendered and "onerror" not in rendered
    rendered = render_sa(_sa_state(nodes=[{
        "name": "node1", "status": "online", "slots_total": 2, "slots_used": 1, "heartbeat_age_s": 5,
        "metrics": {"cpu_pct": "42.4", "disk_free_pct": 61}}]))
    assert "42%" in rendered and "61%" in rendered


def test_sa_credential_expiry_label_uses_configured_days():
    state = _sa_state(accounts=[{"id": "claude-01", "provider": "claude", "node": "node1", "status": "idle",
                                 "credential_expires_at": "2026-10-30T00:00:00+00:00",
                                 "credential_expiry_warning": True, "quota": {}, "roles": []}])
    state["sa"]["thresholds"]["credential_expiry_days"] = 14
    rendered = render_sa(state)
    assert "Expires within 14 days" in rendered and "Expires within 7 days" not in rendered


def test_sa_network_runs_show_attempt_and_retry_derived_from_runs():
    rendered = render_sa(_sa_state(network_issues=[
        {"source": "run", "ts": "2026-10-21T00:10:00+00:00", "ticket": "TESTWP-9", "error_kind": "network",
         "attempts": 2, "retry": False, "detail": "connection reset"},
        {"source": "run", "ts": "2026-10-21T00:00:00+00:00", "ticket": "TESTWP-9", "error_kind": "network",
         "attempts": 1, "retry": True, "detail": "connection reset"}]))
    assert "attempt 2 · not retried yet" in rendered and "attempt 1 · retried" in rendered


def test_delivery_report_button_loads_markdown_into_copyable_drawer():
    page = html()
    assert 'data-act="open-report">Report</button>' in page
    assert "format=md" in page and "loadProjectReport(S.project, epic)" in page
    assert "function drawerReport()" in page and 'data-act="copy-report">Copy Markdown</button>' in page
    assert "else if (act === 'copy-report') copyReport()" in page
    assert "window.navigator.clipboard.writeText(text)" in page and "document.execCommand('copy')" in page


def test_delivery_offers_project_close_and_suggests_it_when_all_tickets_are_done():
    out = json.loads(run_script("""
console.log(JSON.stringify({
  suggested: projectCloseControl({key: 'TESTWP', close_suggested: true}),
  open: projectCloseControl({key: 'TESTWP', close_suggested: false}),
  closed: projectCloseControl({key: 'TESTWP', closed: true, report_version: 2})
}));
"""))
    assert 'All project tickets are Done. Ready to close.' in out["suggested"]
    assert 'data-act="close-project" data-p="TESTWP"' in out["suggested"]
    assert 'data-act="close-project"' in out["open"]
    assert "Closed · study case v2" in out["closed"]


def test_human_review_tab_renders_ticket_pr_diff_verify_preview_and_actions():
    ticket = {
        "key": "TESTFLOW-28", "summary": "Reviewer queue", "description": "Ticket details",
        "jira_url": "https://jira.test/browse/TESTFLOW-28",
        "pr": {"url": "https://github.com/jooservices/testflow/pull/28", "number": 28,
               "title": "Reviewer queue", "additions": 8, "deletions": 2, "changed_files": 3},
        "verify": {"outcome": "human", "verdict": "PASS", "summary": "Verify passed",
                   "checks": "pytest passed", "findings": ""},
        "preview_url": "https://preview.testflow.test",
    }
    driver = f"""
var root = {{innerHTML: ''}};
document.querySelector = function (s) {{ return s === '#view-review' ? root : null; }};
S.reviews = [{json.dumps(ticket)}];
renderReviews();
console.log(root.innerHTML);
"""
    out = run_script(driver)
    for text in ("Human review queue", "TESTFLOW-28", "Ticket details", "pull/28", "Files changed",
                 "Additions", "Deletions", "pytest passed", "Preview environment", "Approve and merge", "Send for rework"):
        assert text in out
    assert 'data-view="review"' in html() and 'id="view-review"' in html()


def test_team_lead_view_selects_epics_and_offers_three_report_formats():
    page = html()
    assert 'data-view="lead">Team Lead</button>' in page
    assert "function renderLead()" in page and "requirement_coverage" in page
    assert "encodeURIComponent(S.leadEpic) + '&format=' + format" in page
    assert "leadReportHref('csv')" in page and "leadReportHref('html')" in page
    assert "Insufficient tasks" in page and "latest completed verify verdict" in page


def test_team_lead_review_and_stakeholder_routes_select_their_console_views():
    routes = run_script("console.log(['/lead', '/review', '/stakeholder'].map(viewOf).join(','));")
    assert routes.strip() == "lead,review,stakeholder"
    page = html()
    assert 'id="view-lead"' in page and 'id="view-review"' in page and 'id="view-stakeholder"' in page
    assert "else if (S.view === 'lead') renderLead();" in page
    assert "else if (S.view === 'review') renderReviews();" in page
    assert "else if (S.view === 'stakeholder') renderStakeholder();" in page
    assert "['cc', 'delivery', 'sa', 'lead', 'business', 'review', 'stakeholder']" in page


def test_team_lead_view_renders_ticket_ac_statuses_metrics_and_exports():
    report = {
        "tickets": {"done": 1, "total": 2, "in_progress": 1, "needs_owner": 0},
        "usage": {"tokens_total": 123, "cost_usd": .045},
        "rework_rounds": {"count": 1},
        "lead_time": {"sample_size": 1, "median_seconds": 86400, "p90_seconds": 86400},
        "requirement_coverage": {
            "insufficient_tasks": True,
            "gaps": [{"ticket": "TESTWP-702", "reason": "done_without_verified_acceptance_criteria"}],
            "tickets": [{
                "key": "TESTWP-701", "type": "Story", "summary": "Child story", "parent_key": "TESTWP-700",
                "status": "Done", "acceptance_criteria": [{"id": "AC1", "text": "Show result", "status": "verified"}],
                "verify": {"verdict": "PASS", "outcome": "merged", "ended_at": "2025-01-02T00:00:00+00:00"},
                "rework_rounds": 0,
            }],
        },
    }
    driver = f"""
var root = {{innerHTML: ''}};
document.querySelector = function (s) {{ return s === '#view-lead' ? root : null; }};
S.data = {{projects: [{{key: 'TESTWP'}}], tickets: [
  {{key: 'TESTWP-700', project: 'TESTWP', type: 'Epic', summary: 'Epic', status: 'To Do', labels: []}}
], recent_runs: [], active_runs: [], incidents: [], events: [], totals: {{}}, versions: {{}}}};
S.project = 'TESTWP'; S.leadEpic = 'TESTWP-700';
S.leadReport = {{project: 'TESTWP', epic: 'TESTWP-700', loading: false, error: '', loadedAt: Date.now(), data: {json.dumps(report)}}};
renderLead();
console.log(root.innerHTML);
"""
    rendered = run_script(driver)
    for value in ("TESTWP-701", "TESTWP-700", "AC1:", "Show result", "Verified", "Done / total", "Needs owner",
                  "Rework rounds", "123", "$0.045000", "format=md", "format=csv", "format=html",
                  "Insufficient tasks"):
        assert value in rendered


def test_gatus_link_is_configurable(monkeypatch):
    monkeypatch.setenv("AIFLOW4_GATUS_URL", "https://status.example/")
    assert Settings.from_env().gatus_url == "https://status.example/"
    monkeypatch.delenv("AIFLOW4_GATUS_URL")
    assert Settings.from_env().gatus_url == "/gatus/"


def test_join_control_url_is_read_from_compose_environment(monkeypatch):
    monkeypatch.setenv("AIFLOW4_JOIN_CONTROL_URL", "https://nodes.example.test/ai-flow")
    assert Settings.from_env().join_control_url == "https://nodes.example.test/ai-flow"


def test_epics_are_sorted_by_the_numeric_part_of_their_key():
    tickets = [
        {"key": "TESTFLOW-19", "project": "TESTFLOW", "type": "Epic", "summary": "Nineteen", "status": "To Do", "labels": []},
        {"key": "TESTFLOW-2", "project": "TESTFLOW", "type": "Epic", "summary": "Two", "status": "To Do", "labels": []},
        {"key": "TESTFLOW-10", "project": "TESTFLOW", "type": "Epic", "summary": "Ten", "status": "To Do", "labels": []},
        {"key": "TESTFLOW-1", "project": "TESTFLOW", "type": "Epic", "summary": "One", "status": "To Do", "labels": []},
    ]
    driver = f"""
var head = {{innerHTML: ''}}, body = {{innerHTML: ''}};
document.querySelector = function (s) {{
  return s === '#dhead' ? head : s === '#dbody' ? body : null;
}};
S.data = {{projects: [{{key: 'TESTFLOW', repo: 'jooservices/testflow', work_minutes: {{target: 30, warn: 45, hard: 60}}}}], tickets: {json.dumps(tickets)},
  recent_runs: [], active_runs: [], incidents: [], events: [], totals: {{}}, versions: {{}}}};
S.project = 'TESTFLOW';
renderDHead();
renderDBody();
console.log(JSON.stringify({{head: head.innerHTML, body: body.innerHTML}}));
"""
    out = json.loads(run_script(driver))
    expected = ["TESTFLOW-1", "TESTFLOW-2", "TESTFLOW-10", "TESTFLOW-19"]
    assert re.findall(r'<option value="(TESTFLOW-\d+)"', out["head"]) == expected
    assert re.findall(r'data-act="epic" data-key="(TESTFLOW-\d+)"', out["body"]) == expected


@pytest.mark.parametrize(
    ("work_minutes", "expected_hard_seconds"),
    [(None, 3600), ({"hard": 90}, 5400)],
)
def test_delivery_card_with_active_run_uses_project_hard_limit(work_minutes, expected_hard_seconds):
    project = {"key": "TESTFLOW", "repo": "jooservices/testflow", "work_minutes": work_minutes}
    ticket = {
        "key": "TESTFLOW-19", "project": "TESTFLOW", "type": "Feature", "summary": "Active work",
        "status": "In Progress", "labels": [], "blocked_by": [],
        "run": {"account": "worker-1", "step": "dev", "fence": 4, "elapsed_s": 120},
    }
    driver = f"""
var body = {{innerHTML: ''}};
document.querySelector = function (s) {{ return s === '#dbody' ? body : null; }};
S.data = {{projects: [{json.dumps(project)}], tickets: [{json.dumps(ticket)}],
  recent_runs: [], active_runs: [], incidents: [], events: [], totals: {{}}, versions: {{}}}};
S.project = 'TESTFLOW';
renderDBody();
console.log(body.innerHTML);
"""
    out = run_script(driver)
    assert 'data-key="TESTFLOW-19"' in out
    assert f'data-hard="{expected_hard_seconds}"' in out


def test_delivery_selection_is_saved_and_restored_from_local_storage():
    prelude = STUBS + """
var store = {'aiflow4-delivery': JSON.stringify({project: 'TESTFLOW', typeFilter: 'Bug', epic: 'TESTFLOW-2'})};
localStorage = {getItem: function (k) { return store[k] || null; }, setItem: function (k, v) { store[k] = String(v); }};
"""
    driver = """
loadPrefs();
var restored = {project: S.project, typeFilter: S.typeFilter, epic: S.epic};
S.project = 'OTHER'; S.typeFilter = 'Feature'; S.epic = 'OTHER-7';
savePrefs();
console.log(JSON.stringify({restored: restored, saved: JSON.parse(store['aiflow4-delivery'])}));
"""
    out = json.loads(run_script(driver, prelude))
    assert out["restored"] == {"project": "TESTFLOW", "typeFilter": "Bug", "epic": "TESTFLOW-2"}
    assert out["saved"] == {"project": "OTHER", "typeFilter": "Feature", "epic": "OTHER-7"}
    page = html()
    assert "loadPrefs();" in page.split(BOOT, 1)[1]
    assert "savePrefs(); render();" in page
    assert "savePrefs(); renderDHead(); renderDBody();" in page
    assert "S.epic = e.target.value; savePrefs();" in page


def test_stakeholder_deployment_links_and_missing_url_fallback():
    links = run_script("console.log(stakeholderEnvLink('develop', 'Develop (UAT)', 'https://uat.example.test')); "
                       "console.log(stakeholderEnvLink('production', 'Production', ''));")
    assert '<a class="btn" href="https://uat.example.test" target="_blank" rel="noopener">Develop (UAT)' in links
    assert "Production: chưa deploy" in links
    page = html()
    assert 'data-view="stakeholder">Stakeholder</button>' in page
    assert "Đúng mong đợi" in page and "Chưa đúng mong đợi" in page
    assert "release_commit" in page and "expected" in page and "actual" in page
    assert "result.key" in page


def test_stakeholder_changes_render_ticket_epic_release_and_verdict_choices():
    driver = """
var body = {innerHTML: ''};
document.querySelector = function (s) { return s === '#sbody' ? body : null; };
S.project = 'TESTWP';
S.sh.project = 'TESTWP';
S.sh.data = {changes: [{key: 'TESTWP-20', summary: 'Stakeholder flow', type: 'Feature', status: 'Human review',
  epic: 'TESTWP-2', changed_at: '2026-10-21T12:00:00+00:00', release_commit: 'v4.2.0',
  change_summary: 'Review these changes'}]};
renderStakeholderBody();
console.log(body.innerHTML);
"""
    rendered = run_script(driver)
    for value in ("TESTWP-20", "Stakeholder flow", "TESTWP-2", "v4.2.0", "Review these changes",
                  "Đúng mong đợi", "Chưa đúng mong đợi", "Ảnh chụp"):
        assert value in rendered


def poll_console(prefs: dict, state: dict) -> dict:
    prelude = STUBS + f"""
var store = {{'aiflow4-delivery': JSON.stringify({json.dumps(prefs)})}};
localStorage = {{getItem: function (k) {{ return store[k] || null; }}, setItem: function (k, v) {{ store[k] = String(v); }}}};
var state = {json.dumps(state)};
fetch = function () {{ return Promise.resolve({{ok: true, json: function () {{ return Promise.resolve(state); }}}}); }};
"""
    driver = """
var els = {};
function el(id) {
  if (!els[id]) els[id] = {innerHTML: '', textContent: '', hidden: false,
    setAttribute: function () {}, getAttribute: function () { return null; },
    removeAttribute: function () {}, classList: {add: function () {}, remove: function () {}},
    appendChild: function () {}, scrollIntoView: function () {}, offsetWidth: 0};
  return els[id];
}
document.querySelector = function (s) { return el(s); };
document.getElementById = function (id) { return el('#' + id); };
document.querySelectorAll = function () { return []; };
var nativeTimeout = setTimeout;
setTimeout = function (fn, ms) { if (ms === 5000 || ms === 15000 || ms === 60000) return; return nativeTimeout(fn, ms); };
location.pathname = '/delivery'; S.view = 'delivery';
loadPrefs();
poll();
setTimeout(function () {
  console.log(JSON.stringify({project: S.project, typeFilter: S.typeFilter, epic: S.epic,
    head: el('#dhead').innerHTML, body: el('#dbody').innerHTML,
    saved: JSON.parse(store['aiflow4-delivery'])}));
}, 30);
"""
    return json.loads(run_script(driver, prelude))


def test_poll_falls_back_to_the_first_project_and_clears_a_stale_epic():
    state = {
        "projects": [{"key": "TESTFLOW", "repo": "jooservices/testflow"}, {"key": "OTHER", "repo": "jooservices/other"}],
        "tickets": [
            {"key": "TESTFLOW-2", "project": "TESTFLOW", "type": "Epic", "summary": "Two", "status": "To Do", "labels": []},
            {"key": "TESTFLOW-3", "project": "TESTFLOW", "type": "Feature", "summary": "Work", "status": "To Do", "labels": []},
        ],
        "recent_runs": [], "active_runs": [], "incidents": [], "events": [], "totals": {}, "versions": {},
    }
    out = poll_console({"project": "GONE", "typeFilter": "all", "epic": "GONE-9"}, state)
    assert out["project"] == "TESTFLOW"
    assert out["epic"] == "all"
    assert out["typeFilter"] == "all"
    assert "TESTFLOW-3" in out["body"]
    assert out["saved"] == {"project": "TESTFLOW", "typeFilter": "all", "epic": "all"}


def test_poll_clears_a_stale_epic_and_type_filter_in_the_current_project():
    state = {
        "projects": [{"key": "TESTFLOW", "repo": "jooservices/testflow"}],
        "tickets": [
            {"key": "TESTFLOW-2", "project": "TESTFLOW", "type": "Epic", "summary": "Two", "status": "To Do", "labels": []},
            {"key": "TESTFLOW-3", "project": "TESTFLOW", "type": "Feature", "summary": "Work", "status": "To Do", "labels": []},
        ],
        "recent_runs": [], "active_runs": [], "incidents": [], "events": [], "totals": {}, "versions": {},
    }
    out = poll_console({"project": "TESTFLOW", "typeFilter": "Bug", "epic": "TESTFLOW-99"}, state)
    assert out["project"] == "TESTFLOW"
    assert out["epic"] == "all"
    assert out["typeFilter"] == "all"
    assert "TESTFLOW-3" in out["body"]
    assert out["saved"] == {"project": "TESTFLOW", "typeFilter": "all", "epic": "all"}


def test_delivery_selection_storage_errors_are_ignored():
    prelude = STUBS + """
localStorage = {getItem: function () { throw new Error('storage off'); },
                setItem: function () { throw new Error('storage off'); }};
"""
    driver = """
loadPrefs();
savePrefs();
console.log(JSON.stringify({project: S.project, typeFilter: S.typeFilter, epic: S.epic}));
"""
    out = json.loads(run_script(driver, prelude))
    assert out == {"project": None, "typeFilter": "all", "epic": "all"}
