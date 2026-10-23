"""Console page: node registration details, full-width layout, global footer, no new libraries."""
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from aiflow.v4 import VERSION
from aiflow.v4.control.console_auth import COOKIE_NAME, ROLE_PERMISSIONS, issue_session
from aiflow.v4.control.ui import CONSOLE_PAGE_PATHS
from aiflow.v4.core.settings import Settings

PAGE = Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "ui" / "console.html"
BOOT = "/* ---------- boot ---------- */"
STUBS = """
var document = {addEventListener: function () {}, documentElement: {setAttribute: function () {},
  classList: {add: function () {}, remove: function () {}}},
  getElementById: function () { return null; }, querySelector: function () { return null; },
  querySelectorAll: function () { return []; }};
var window = {matchMedia: function () { return {matches: false}; }, addEventListener: function () {}};
var location = {pathname: '/cc'};
var localStorage = {getItem: function () { return null; }, setItem: function () {}};
var fetch = function () {};
var setInterval = function () {};
"""
OWNER_PERMISSIONS = sorted(ROLE_PERMISSIONS["owner"])
LEAD_PERMISSIONS = sorted(ROLE_PERMISSIONS["lead"])


def console_session_user(role: str = "owner", permissions: list[str] | None = None) -> dict:
    return {"authenticated": True, "user": role + "@example.test", "role": role,
            "permissions": permissions if permissions is not None else sorted(ROLE_PERMISSIONS[role])}


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


def console_session(role: str) -> dict:
    return {"authenticated": True, "user": role, "role": role,
            "permissions": sorted(ROLE_PERMISSIONS[role])}


def render_drawer(node: dict, console_user: dict | None = None, operations: list[dict] | None = None) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the console drawer")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    if console_user and "permissions" not in console_user:
        console_user = {**console_user, "permissions": LEAD_PERMISSIONS if console_user.get("role") == "lead"
                        else OWNER_PERMISSIONS}
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


def render_knowledge(data: dict, history: dict | None = None, user: dict | None = None) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the Knowledge view")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    driver = f"""
var knowledgeView = {{innerHTML: ''}};
document.querySelector = function (selector) {{ return selector === '#view-knowledge' ? knowledgeView : null; }};
S.knowledge.data = {json.dumps(data)};
S.knowledge.actor = 'Mai Nguyen';
S.knowledge.history = {json.dumps(history)};
S.consoleUser = {json.dumps(user or console_session_user())};
renderKnowledge();
console.log(knowledgeView.innerHTML);
"""
    result = subprocess.run([node_js, "-"], input=STUBS + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def render_projects(projects: list[dict], catalog: dict, preflight: dict | None = None,
                    user: dict | None = None, repository_editor: dict | None = None) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the Projects screen")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    driver = f"""
var projectsView = {{innerHTML: ''}};
document.querySelector = function (selector) {{ return selector === '#view-projects' ? projectsView : null; }};
S.projectAdmin.projects = {json.dumps(projects)};
S.projectAdmin.catalog = {json.dumps(catalog)};
S.projectAdmin.showForm = true;
S.projectAdmin.preflight = {json.dumps(preflight)};
S.projectAdmin.repositoryEditor = {json.dumps(repository_editor)};
S.consoleUser = {json.dumps(user or console_session("owner"))};
renderProjects();
console.log(projectsView.innerHTML);
"""
    result = subprocess.run([node_js, "-"], input=STUBS + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def render_dashboard(state: dict, analytics: list[dict] | None = None,
                     weekly_report: dict | None = None) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the console Dashboard")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    project_key = (state.get("projects") or [{}])[0].get("key")
    report = weekly_report or {"week_start": "2026-10-19", "trend": [], "finished_without_person": [],
                               "needed_person": [], "repeated_failures": []}
    driver = f"""
var dashboard = {{innerHTML: ''}};
document.querySelector = function (selector) {{ return selector === '#view-cc' ? dashboard : null; }};
S.data = {json.dumps(state)};
S.analytics = {json.dumps(analytics or [])};
S.project = {json.dumps(project_key)};
S.consoleUser = {json.dumps(console_session('owner'))};
S.autonomy.data = {{budget: {{used_minutes: 12, daily_budget_minutes: 90, remaining_minutes: 78}}, report: {json.dumps(report)}}};
renderDashboard();
console.log(dashboard.innerHTML);
"""
    result = subprocess.run([node_js, "-"], input=STUBS + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def render_machines(state: dict) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the Machines and AI view")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    driver = f"""
var machines = {{innerHTML: ''}};
document.querySelector = function (selector) {{ return selector === '#view-machines' ? machines : null; }};
S.data = {json.dumps(state)};
S.services = [];
S.view = 'machines';
S.consoleUser = {json.dumps(console_session('owner'))};
renderMachines();
console.log(machines.innerHTML);
"""
    result = subprocess.run([node_js, "-"], input=STUBS + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def render_integrations(user: dict, jira: dict | None = None) -> str:
    driver = f"""
var integrationsView = {{innerHTML: ''}};
document.querySelector = function (selector) {{ return selector === '#view-integrations' ? integrationsView : null; }};
S.view = 'integrations';
S.consoleUser = {json.dumps(user)};
S.integ.jira = {json.dumps(jira)};
S.integrations = {{data: {{connected: false}}, pending: null, installations: [], selected: '', busy: false}};
renderIntegrations();
console.log(integrationsView.innerHTML);
"""
    return run_script(driver)


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
    owner = render_drawer(node, console_session("owner"))
    assert 'data-act="node-install"' in owner and 'data-act="node-repair"' in owner
    assert "worker-api.service" in owner and "health check configured" in owner
    lead = render_drawer(node, console_session("lead"))
    assert 'data-act="node-install"' not in lead and 'data-act="node-repair"' not in lead
    assert "Only an authenticated user with node management permission can install or repair a node." in lead
    no_agent = dict(node, status="joined", has_agent=False)
    join = render_drawer(no_agent, console_session("owner"))
    assert 'data-act="join-command"' in join


def test_node_operation_log_escapes_service_names_from_saved_results():
    service_name = '<img src=x onerror="alert(1)">'
    node = {"name": "node3", "ip": "192.0.2.55", "status": "online", "metrics": {}, "tools": {},
            "has_agent": True, "projects": [], "heartbeat_age_s": 12}
    rendered = render_drawer(node, console_session("lead"), [{
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

    ctx.settings.console_login = True
    ctx.settings.console_session_secret = "console-page-test-secret-at-least-32-bytes"
    app = create_app(ctx)
    mount_ui(app, ctx)  # as the control-api CLI does
    app.state.sync_accounts()
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(COOKIE_NAME, issue_session("console@example.test", "owner", 2_000_000_000,
                                                   ctx.settings.console_session_secret))
    page = client.get("/cc")
    assert page.status_code == 200 and 'id="footer"' in page.text
    for path in CONSOLE_PAGE_PATHS:
        response = client.get(path)
        assert response.status_code != 404, f"{path} returned 404"
        if path == "/":
            assert (response.status_code, response.headers["location"]) in {
                (302, "/cc"), (307, "/dashboard")
            }
        elif path == "/help":
            assert response.status_code == 307 and response.headers["location"] == "/help/"
        else:
            assert response.status_code == 200, f"{path} did not load the console"
    assert client.get("/help", follow_redirects=True).status_code == 200
    assert 'id="view-sa"' in page.text and 'data-view="sa"' in page.text
    lead = client.get("/lead")
    assert lead.status_code == 200 and 'id="tab-lead"' in lead.text and 'id="view-lead"' in lead.text
    s = client.get("/v1/state").json()
    assert s["version"] == VERSION
    assert s["env"] and "control_repo" in s and "versions" in s
    assert s["links"] == {"o2": "/o2/", "mail": "/mail/", "gatus": "/gatus/"}


def test_support_screen_shows_ticket_picker_and_review_inventory_before_download():
    rendered = run_script(f"""
var root = {{innerHTML: ''}};
document.querySelector = function (selector) {{ return selector === '#view-support' ? root : null; }};
S.view = 'support'; S.consoleUser = {json.dumps(console_session_user('owner'))};
S.data = {{tickets: [{{key: 'TESTWP-7', type: 'Bug', summary: 'Example issue'}}]}};
S.support = {{ticketKey: 'TESTWP-7', busy: false, error: '', bundle: {{download_url: '/v1/support/bundles/random/download',
  inventory: {{created_at: '2026-10-22T10:00:00+00:00', scan: 'passed', files: [{{name: 'incidents.json', bytes: 90}}]}}}}}};
renderSupport(); console.log(root.innerHTML);
""")
    assert 'id="support-ticket"' in rendered and "TESTWP-7" in rendered
    assert "Review bundle inventory" in rendered and "Sensitive data scan passed" in rendered
    assert "Download reviewed bundle" in rendered and "/v1/support/bundles/random/download" in rendered


def test_traefik_control_router_covers_new_console_paths_and_excludes_monitoring_and_logs():
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "deploy/v4/control/compose.yaml").read_text(encoding="utf-8"))
    rule = compose["services"]["control-api-a"]["labels"]["traefik.http.routers.api.rule"]
    assert rule == "PathPrefix(`/`) && !PathPrefix(`/o2`) && !PathPrefix(`/gatus`)"
    assert "PathPrefix(`/v1`)" not in rule  # the broad rule also carries the control API
    for path in CONSOLE_PAGE_PATHS:
        if path not in {"/", "/help", "/notifications"}:
            assert f"PathPrefix(`{path}`)" not in rule and f"Path(`{path}`)" not in rule


def test_console_reload_selects_the_view_from_each_existing_address():
    views = ("cc", "work", "delivery", "sa", "machines", "users", "lead", "business", "review",
             "stakeholder", "knowledge", "backup", "integrations", "projects", "licensing", "notifications",
             "support")
    page_paths = tuple(path for path in CONSOLE_PAGE_PATHS if path not in {"/", "/help"})
    for path in page_paths:
        expected_view = ("cc" if path in {"/cc", "/dashboard"} else
                         "work" if path == "/board" else path.removeprefix("/"))
        prelude = STUBS.replace("pathname: '/cc'", f"pathname: {json.dumps(path)}") + f"""
var viewNames = {json.dumps(views)};
var tabs = viewNames.map(function (view) {{
  var attrs = {{'data-view': view}};
  return {{setAttribute: function (key, value) {{ attrs[key] = value; }},
           getAttribute: function (key) {{ return attrs[key]; }}}};
}});
var panels = {{}};
viewNames.forEach(function (view) {{ panels[view] = {{hidden: true}}; }});
document.querySelectorAll = function (selector) {{ return selector === '.tab' ? tabs : []; }};
document.querySelector = function (selector) {{
  var match = /^#view-(.+)$/.exec(selector);
  return match ? panels[match[1]] : null;
}};
"""
        state = json.loads(run_script(
            "showView(); console.log(JSON.stringify({path: location.pathname, view: S.view, "
            "selected: tabs.filter(function (tab) { return tab.getAttribute('aria-selected') === 'true'; })[0] "
            "&& tabs.filter(function (tab) { return tab.getAttribute('aria-selected') === 'true'; })[0]"
            ".getAttribute('data-view'), visible: Object.keys(panels).filter(function (view) { return !panels[view].hidden; })}));",
            prelude=prelude,
        ))
        assert state == {"path": path, "view": expected_view, "selected": expected_view,
                         "visible": [expected_view]}, f"{path} did not select its view after a page reload"


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
        "repository": "jooservices/testflow",
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
    S.reviews = [{json.dumps(ticket)}, {{key: 'TESTFLOW-29', summary: 'AI question', status: 'Needs owner',
      kind: 'question_from_ai', actions: ['answer', 'ask-ai'], jira_url: 'https://jira.test/browse/TESTFLOW-29',
      pr: {{}}, verify: {{}}, thread: [{{kind: 'question', author: 'Reviewer', text: 'Can retries be preserved?'}}]}}];
S.reviewMetrics = {{people_needed_this_week: 2, people_needed_eight_weeks_ago: 1, finished_with_no_person_pct: 50,
  median_wait_minutes: 10, approved_first_time_pct: 75, sent_back_or_rejected_pct: 25,
  weekly: [{{week: '2026-10-19', total: 2, by_reason: {{question_from_ai: 1, decision_needed: 1,
    review_before_merge: 0, blocked: 0}}}}], suggestions: [{{reason: 'question_from_ai', suggestion: 'Clarify repeated questions.'}}]}};
S.reviewProjects = ['TESTFLOW', 'TESTWP'];
renderReviews();
console.log(root.innerHTML);
"""
    out = run_script(driver)
    for text in ("Human review queue", "TESTFLOW-28", "Ticket details", "pull/28", "Files changed",
                 "Repository", "jooservices/testflow", "Additions", "Deletions", "pytest passed",
                 "Preview environment", "Approve and merge", "Reject", "Send for rework", "Skip gate and merge",
                 "Answer", "Ask AI a question", "Can retries be preserved?", "Changes to reduce each reason",
                 "Approved first time", "Export metrics CSV", "Project"):
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
    routes = run_script("console.log(['/dashboard', '/work', '/delivery', '/machines', '/users', '/sa', '/lead', '/business', '/review', '/stakeholder', '/knowledge', '/backup', '/integrations', '/projects', '/licensing', '/notifications', '/support']"
                        ".map(viewOf).join(','));")
    assert routes.strip() == "cc,work,delivery,machines,users,sa,lead,business,review,stakeholder,knowledge,backup,integrations,projects,licensing,notifications,support"
    page = html()
    assert 'id="view-lead"' in page and 'id="view-review"' in page and 'id="view-stakeholder"' in page
    assert 'id="view-integrations"' in page and 'id="tab-integrations"' in page
    assert 'id="view-notifications"' in page and 'id="tab-notifications"' in page
    assert page.count('id="view-integrations"') == 1 and page.count('id="tab-integrations"') == 1
    assert "else if (S.view === 'lead') renderLead();" in page
    assert "else if (S.view === 'review') renderReviews();" in page
    assert "else if (S.view === 'stakeholder') renderStakeholder();" in page
    assert "else if (S.view === 'integrations') renderIntegrations();" in page
    assert ("var VIEW_GROUPS = [['cc', 'work'], ['delivery', 'sa', 'lead', 'business', 'review', 'stakeholder'],\n"
            "  ['machines', 'users', 'knowledge', 'backup', 'integrations', 'projects', 'licensing', 'notifications', 'support']];"
            ) in page
    assert "else if (S.view === 'notifications') renderNotifications();" in page
    assert ("['cc', 'work', 'delivery', 'sa', 'lead', 'business', 'review', 'stakeholder', 'machines', 'users', "
            "'knowledge', 'backup', 'integrations', 'projects', 'licensing', 'notifications', 'support']") in page
    assert "{ view: 'support', label: 'Support', permission: 'console.manage' }" in page
    assert "licensing: 'licensing.manage'" in page


def test_integrations_view_shows_the_connected_site_and_never_renders_secret_values():
    status = {"connected": True, "source": "oauth", "state": "connected", "needs_reconnect": False, "kind": "oauth",
              "site_name": "acme", "site_url": "https://acme.atlassian.net", "cloud_id": "cloud-1",
              "account": "bot@acme.test", "scopes": ["read:jira-work", "write:jira-work", "offline_access"],
              "expires_at": "2026-10-21T10:00:00+00:00", "connected_at": "2026-10-21T09:00:00+00:00",
              "connected_by": "owner@example.test", "error": "", "disconnected_at": "", "disconnected_by": "",
              "legacy_owner_env": False, "oauth_app": {"configured": True, "client_id": "client-1"},
              "redirect_uri": "https://control.example.test/v1/integrations/jira/oauth/callback",
              "requested_scopes": ["read:jira-work", "write:jira-work", "offline_access"]}
    def render(data: dict, permissions: list[str] | None = None) -> str:
        return run_script("var root = {innerHTML: ''};"
                          " document.querySelector = function (s) { return s === '#view-integrations' ? root : null; };"
                          f" S.view = 'integrations'; S.consoleUser = {{permissions: {json.dumps(permissions if permissions is not None else sorted(ROLE_PERMISSIONS['owner']))}}};"
                          f" S.integ.jira = {json.dumps(data)}; renderIntegrations();"
                          " console.log(root.innerHTML);")

    out = render(status)
    for text in ("Connect GitHub", "Create a GitHub App", "acme", "cloud-1", "bot@acme.test",
                 "write:jira-work", "Disconnect", "Connect Jira", "/v1/integrations/jira/oauth/callback"):
        assert text in out
    assert 'type="password" data-integ-field="client_secret"' in out and 'type="password" data-integ-field="api_token"' in out
    needs = render({**status, "connected": False, "needs_reconnect": True, "state": "needs_reconnect"})
    assert "Reconnect needed" in needs and "Press Connect Jira again" in needs
    assert "Connect GitHub" in render(status, ["integrations.manage"])


def test_knowledge_view_renders_resources_history_and_owner_approval_diff():
    data = {
        "resources": [{"type": "knowledge", "id": "ops", "title": "Ops notes", "content": "after",
                       "archived": False, "version": 2, "updated_by": "Mai Nguyen", "updated_at": "2026-10-21T00:00:00Z"}],
        "proposals": [{"proposal_id": 7, "type": "policy", "id": "default", "title": "Default policy",
                       "content": "allow: []", "base_content": "allow: [git status]", "base_version": 2,
                       "archived": False, "actor": "Mai Nguyen", "created_at": "2026-10-21T00:00:00Z",
                       "pr_number": 19, "pr_url": "https://github.com/jooservices/aiflow-control/pull/19"}],
    }
    history = {"type": "knowledge", "id": "ops", "history": [
        {"version": 2, "content": "after", "title": "Ops notes", "actor": "An Tran", "action": "save",
         "created_at": "2026-10-21T00:00:00Z", "archived": False},
        {"version": 1, "content": "before", "title": "Ops notes", "actor": "Mai Nguyen", "action": "save",
         "created_at": "2026-10-20T00:00:00Z", "archived": False},
    ], "proposals": []}
    output = render_knowledge(data, history)
    assert "Ops notes" in output and "knowledge-history" in output
    assert "Before · v1" in output and "After · Ops notes" in output
    assert "Policy approvals" in output and "allow: [git status]" in output and "Approve and merge policy" in output


def test_super_admin_session_shows_github_connect_and_project_resume_controls(ctx):
    from fastapi.testclient import TestClient
    from aiflow.v4.control.api import create_app
    from aiflow.v4.control.console_auth import issue_session

    secret = "test-console-session-secret-at-least-32-bytes"
    ctx.settings.console_session_secret = secret
    client = TestClient(create_app(ctx))
    client.cookies.set("aiflow4_session", issue_session("admin@example.test", "super_admin", 2_000_000_000,
                                                         secret))
    session = client.get("/v1/console/session")
    assert session.status_code == 200
    user = session.json()
    assert user["role"] == "administrator"
    assert "*" in user["permissions"]

    jira = {"connected": False, "source": "none", "state": "disconnected", "needs_reconnect": False,
            "oauth_app": {"configured": False}, "site_url": "", "requested_scopes": [], "redirect_uri": ""}
    integrations = render_integrations(user, jira)
    projects = render_projects([
        {"key": "ACTIVE", "repo": "acme/active", "status": "active", "priority": 1, "ticket_count": 0},
        {"key": "NEWAPP", "repo": "acme/new-app", "status": "paused", "priority": 2, "ticket_count": 0},
    ], {}, user=user)
    node = render_drawer({"name": "node1", "ip": "192.0.2.1", "status": "online", "metrics": {},
                          "tools": {}, "has_agent": True, "projects": [], "heartbeat_age_s": 10}, user)
    knowledge = render_knowledge({
        "resources": [{"type": "knowledge", "id": "ops", "title": "Ops notes", "content": "notes",
                        "archived": False, "version": 1, "updated_by": "Admin", "updated_at": None}],
        "proposals": [{"proposal_id": 7, "type": "policy", "id": "default", "title": "Default policy",
                       "content": "allow: []", "base_content": "allow: []", "base_version": 1,
                       "archived": False, "actor": "Admin", "created_at": None, "pr_number": 1,
                       "pr_url": "https://github.com/example/control/pull/1"}],
    }, user=user)
    assert ">Connect GitHub</button>" in integrations
    assert "Use an existing GitHub App" in integrations
    assert "data-github-app-id" in integrations and "data-github-key-file" in integrations
    assert ">Connect Jira</button>" in integrations
    assert "Add project" in projects and ">Pause</button>" in projects
    assert "Resume</button>" in projects and "Remove</button>" in projects
    assert 'data-act="node-install"' in node and 'data-act="node-repair"' in node
    assert "New resource" in knowledge and 'data-act="knowledge-edit"' in knowledge
    assert 'data-act="knowledge-approve"' in knowledge


def test_super_admin_can_sign_in_and_see_github_and_project_controls_in_a_browser(ctx):
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error, sync_playwright
    import uvicorn

    from aiflow.v4.control import login
    from aiflow.v4.control.api import create_app
    from aiflow.v4.control.ui import mount_ui

    try:
        playwright = sync_playwright().start()
    except Error as exc:
        pytest.skip(f"Playwright cannot start on this node: {exc}")
    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Error as exc:
            pytest.skip(f"Playwright Chromium cannot launch on this node: {exc}")

        ctx.settings.console_login = True
        ctx.settings.console_session_secret = "browser-test-session-secret-at-least-32-bytes"
        ctx.repo().projects["TESTWP"].paused = True
        password = login.seed_user(ctx.db, "browser-admin@example.test", "super_admin", "Browser Admin")
        assert password
        app = create_app(ctx)
        mount_ui(app, ctx)
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(128)
        base_url = f"http://127.0.0.1:{server_socket.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        server_thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)
        try:
            server_thread.start()
            deadline = time.monotonic() + 10
            while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "console test server did not start"

            page = browser.new_page()
            page.goto(base_url + "/login?next=%2Fintegrations", wait_until="domcontentloaded")
            page.get_by_label("Email").fill("browser-admin@example.test")
            page.get_by_label("Password").fill(password)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url("**/cc")
            page.get_by_role("tab", name="Integrations").click()
            github_app_setup = page.locator(
                "#view-integrations summary, #view-integrations a[href*='installation-settings']")
            github_app_setup.first.wait_for(state="visible", timeout=10000)
            existing_app = page.locator("#view-integrations summary").filter(has_text="Use an existing GitHub App")
            if existing_app.count():
                existing_app.click()
                page.locator("#view-integrations [data-github-app-id]").wait_for(state="visible")
                page.locator("#view-integrations [data-github-key-file]").wait_for(state="visible")
            else:
                page.get_by_role("link", name="Add repositories on GitHub").wait_for(state="visible", timeout=10000)

            page.get_by_role("tab", name="Projects").click()
            page.get_by_role("button", name="Resume", exact=True).wait_for(state="visible", timeout=10000)

            # Direct navigation and reload must leave each existing view selected at the same address.
            for path in (p for p in CONSOLE_PAGE_PATHS if p not in {"/", "/help"}):
                view = ("cc" if path in {"/cc", "/dashboard"} else
                        "work" if path == "/board" else path.removeprefix("/"))
                page.goto(base_url + path, wait_until="domcontentloaded")
                for action in ("load", "reload"):
                    assert page.url == base_url + path
                    selected = page.evaluate("""view => {
                      const tab = document.querySelector('#tab-' + view);
                      const panel = document.querySelector('#view-' + view);
                      return !!tab && tab.getAttribute('aria-selected') === 'true' && !!panel && !panel.hidden;
                    }""", view)
                    assert selected, f"{path} did not keep the {view} view selected after {action}"
                    if action == "load":
                        page.reload(wait_until="domcontentloaded")
        finally:
            browser.close()
            server.should_exit = True
            if server_thread.is_alive():
                server_thread.join(timeout=5)
            server_socket.close()
    finally:
        playwright.stop()


def test_role_navigation_and_forbidden_pages_in_a_browser_for_three_roles(ctx):
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error, sync_playwright
    import uvicorn

    from aiflow.v4.control import login
    from aiflow.v4.control.api import create_app

    try:
        playwright = sync_playwright().start()
    except Error as exc:
        pytest.skip(f"Playwright cannot start on this node: {exc}")
    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Error as exc:
            pytest.skip(f"Playwright Chromium cannot launch on this node: {exc}")
        ctx.settings.console_login = True
        ctx.settings.console_session_secret = "browser-role-test-secret-at-least-32-bytes"
        people = [
            ("delivery@example.test", "delivery", "/delivery", ["Dashboard", "Work board", "Delivery", "Human review"], "/sa"),
            ("ba@example.test", "ba", "/business", ["Dashboard", "Work board", "Business", "Knowledge"], "/review"),
            ("stakeholder@example.test", "stakeholder", "/stakeholder",
             ["Dashboard", "Work board", "Stakeholder"], "/integrations"),
        ]
        passwords = {email: login.seed_user(ctx.db, email, role, role.title()) for email, role, *_ in people}
        app = create_app(ctx)
        from aiflow.v4.control.ui import mount_ui
        mount_ui(app, ctx)
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(128)
        base_url = f"http://127.0.0.1:{server_socket.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        server_thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)
        try:
            server_thread.start()
            deadline = time.monotonic() + 10
            while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "console test server did not start"
            for email, _role, landing, visible, forbidden in people:
                page = browser.new_page()
                page.goto(base_url + "/login", wait_until="domcontentloaded")
                page.get_by_label("Email").fill(email)
                page.get_by_label("Password").fill(passwords[email])
                page.get_by_role("button", name="Sign in").click()
                page.wait_for_url("**" + landing)
                for label in visible:
                    page.get_by_role("tab", name=label).wait_for(state="visible", timeout=10000)
                for label in ("SA", "Machines and AI", "Integrations", "Projects", "Users and roles"):
                    assert page.get_by_role("tab", name=label).count() == 0 or not page.get_by_role("tab", name=label).is_visible()
                page.goto(base_url + forbidden, wait_until="domcontentloaded")
                assert "This view is not available for your role." in page.locator("body").inner_text()
                page.close()
        finally:
            browser.close()
            server.should_exit = True
            if server_thread.is_alive():
                server_thread.join(timeout=5)
            server_socket.close()
    finally:
        playwright.stop()


def test_lead_sees_only_read_permitted_controls():
    lead = console_session_user("lead")
    integrations = render_integrations(lead)
    projects = render_projects([{"key": "NEWAPP", "repo": "acme/new-app", "status": "paused",
                                 "priority": 1, "ticket_count": 0}], {}, user=lead)
    knowledge = render_knowledge({"resources": [{"type": "knowledge", "id": "ops", "title": "Ops notes",
                                                   "content": "notes", "archived": False, "version": 1,
                                                   "updated_by": "owner", "updated_at": None}],
                                  "proposals": []}, user=lead)
    assert "Only a role with integration management permission" in integrations
    assert "Connect GitHub" not in integrations and "Connect Jira" not in integrations
    assert "Resume" not in projects and "Add project" not in projects and "Remove" not in projects
    assert "Ops notes" in knowledge and 'data-act="knowledge-history"' in knowledge
    assert 'data-act="knowledge-edit"' not in knowledge and 'data-act="knowledge-approve"' not in knowledge


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
    assert out["saved"] == {"project": "OTHER", "typeFilter": "Feature", "aiFilter": "all",
                            "epic": "OTHER-7", "starredProjects": []}
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
    assert out["saved"] == {"project": "TESTFLOW", "typeFilter": "all", "aiFilter": "all",
                            "epic": "all", "starredProjects": []}


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
    assert out["saved"] == {"project": "TESTFLOW", "typeFilter": "all", "aiFilter": "all",
                            "epic": "all", "starredProjects": []}


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


def test_projects_screen_lists_lifecycle_fields_and_preflight_controls():
    rendered = render_projects(
        [{"key": "NEWAPP", "repo": "jooservices/new-app", "status": "paused", "priority": 3,
          "ticket_count": 12}],
        {"jira_sites": ["https://jira.example"], "jira_projects": [{"key": "NEWAPP", "name": "New App"}],
         "github_repos": [{"full_name": "jooservices/new-app"}], "stacks": ["python-3.12"]},
        {"ok": False, "missing_statuses": ["Intake", "Needs owner"], "branch_exists": True,
         "nodes": [{"name": "node1", "ready": False, "missing_tools": ["python3"]}]})
    for text in ("Projects", "NEWAPP", "jooservices/new-app", "paused", "Priority", "Tickets", "Resume", "Remove",
                 "Jira project key or URL", "Connected Jira site", "GitHub repository", "develop", "python-3.12",
                 "Merge method", "Checks, one command per line", "Missing Jira workflow statuses:", "Intake",
                 "Needs owner", "node1: missing python3", "Run preflight", "Save project"):
        assert text in rendered
    assert 'data-act="project-pause"' in rendered and 'data-act="project-remove"' in rendered


def test_projects_editor_renders_live_epics_and_repository_specific_settings():
    editor = {"key": "JOOWP", "busy": False, "saving": False, "error": "", "data": {
        "default_repo": "jooservices/wordpress-core",
        "project_defaults": {"base": "develop", "checks": ["composer install"], "stack": "php-8.3"},
        "repositories": [
            {"repo": "jooservices/wordpress-core", "base": "main", "checks": ["vendor/bin/phpunit"],
             "stack": "php-8.3"},
            {"repo": "jooservices/wordpress-mcp", "base": "", "checks": [], "stack": ""},
        ],
        "epic_repositories": {"JOOWP-306": "jooservices/wordpress-mcp"},
        "epics": [{"key": "JOOWP-306", "summary": "MCP", "status": "To Do"}],
        "github_repos": [{"full_name": "jooservices/wordpress-core"},
                         {"full_name": "jooservices/wordpress-mcp"}],
        "stacks": ["node-24", "php-8.3"],
    }}
    rendered = render_projects([{"key": "JOOWP", "repo": "jooservices/wordpress-core",
                                 "repositories": ["jooservices/wordpress-core", "jooservices/wordpress-mcp"],
                                 "status": "active", "priority": 5, "ticket_count": 2}], {},
                              repository_editor=editor)
    assert "JOOWP-306" in rendered and "MCP" in rendered
    assert "jooservices/wordpress-mcp" in rendered and "Inherit develop" in rendered
    assert "Save repository settings" in rendered and "data-epic-repository" in rendered


def test_projects_editor_submits_the_displayed_connection_for_a_stale_assignment():
    repo = {"repo": "person/tool", "base": "develop", "checks": [], "stack": "",
            "connection": "personal", "assigned_connection": "default", "connection_stale": True,
            "connections": ["personal"]}
    editor = {"key": "TESTWP", "busy": False, "saving": False, "error": "", "data": {
        "default_repo": "person/tool", "project_defaults": {"base": "develop", "checks": [], "stack": ""},
        "repositories": [repo], "epic_repositories": {}, "epics": [], "github_repos": [], "stacks": [],
        "connections": [{"id": "default", "label": "App", "account": "jooservices", "kind": "app"},
                        {"id": "personal", "label": "Personal", "account": "person", "kind": "token"}],
    }}
    rendered = render_projects([], {}, repository_editor=editor)
    assert "Previously assigned to <span class=\"mono\">default</span>" in rendered
    assert '<option value="personal" selected>Personal · person · access token</option>' in rendered

    driver = f"""
render = function () {{}};
toast = function () {{}};
loadProjectList = function () {{}};
poll = function () {{}};
var sent = null;
fetch = function (url, options) {{
  sent = JSON.parse(options.body);
  return Promise.resolve({{ok: true, json: function () {{ return Promise.resolve({{saved: true, commit: 'commit-1'}}); }}}});
}};
S.projectAdmin.repositoryEditor = {json.dumps(editor)};
saveProjectRepositories();
setTimeout(function () {{ console.log(JSON.stringify(sent)); }}, 0);
"""
    submitted = json.loads(run_script(driver))
    assert submitted["repositories"][0]["connection"] == "personal"


def test_ticket_cards_business_change_requests_and_drawer_show_selected_repository():
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the console")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    driver = r"""
var ticket = {key: 'JOOWP-4', project: 'JOOWP', summary: 'MCP change', status: 'To Do', type: 'Story',
  priority: 'Medium', labels: [], blocked_by: [], repository: 'jooservices/wordpress-mcp', parent_key: 'JOOWP-306'};
S.data = {tickets: [ticket], projects: [], nodes: [], accounts: [], active_runs: [], recent_runs: [],
  incidents: [], events: [], totals: {}, versions: {}};
S.ticket[ticket.key] = {ticket: ticket, runs: [], events: [], writes: [], timeline: null, jira_url: ''};
console.log(card(ticket, {work_minutes: {hard: 60}}));
console.log(crCard({key: ticket.key, summary: ticket.summary, epic: ticket.parent_key, status: ticket.status}));
console.log(drawerTicket(ticket.key));
"""
    result = subprocess.run([node_js, "-"], input=STUBS + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("jooservices/wordpress-mcp") >= 3
    assert "Repository" in result.stdout


def test_projects_screen_is_mounted_and_uses_owner_protected_project_endpoints():
    page = html()
    assert 'data-view="projects">Projects</button>' in page
    assert 'id="view-projects"' in page and "/v1/projects/catalog" in page
    assert "/v1/projects/preflight" in page and "method: 'DELETE'" in page
    assert "parallel_limit" in page and "merge_method" in page


def test_command_center_menu_order_and_role_visibility():
    page = html()
    tabs = re.findall(r'<button class="tab"[^>]*data-view="([^"]+)">([^<]+)</button>', page)
    assert tabs == [
        ("cc", "Dashboard"), ("work", "Work board"), ("delivery", "Delivery"), ("sa", "SA"),
        ("lead", "Team Lead"), ("business", "Business"), ("review", "Human review"),
        ("stakeholder", "Stakeholder"), ("machines", "Machines and AI"), ("users", "Users and roles"),
        ("knowledge", "Knowledge"), ("backup", "Backup &amp; Restore"), ("integrations", "Integrations"),
        ("projects", "Projects"), ("licensing", "Licence"), ("notifications", "Notifications"),
        ("support", "Support"),
    ]
    assert "data-view=\"cc\">Command Center</button>" not in page
    # Each role sees exactly the views its permissions in console_auth.py allow.
    permission_for_view = {
        "cc": "view.dashboard", "work": "view.work_board", "delivery": "view.delivery", "sa": "view.sa",
        "lead": "view.team_lead", "business": "view.business", "review": "view.human_review",
        "stakeholder": "view.stakeholder", "machines": "view.machines_ai", "users": "console.manage",
        "knowledge": "view.knowledge", "integrations": "view.integrations", "projects": "projects.manage",
        "backup": "backup.read", "licensing": "licensing.manage", "notifications": "notifications.manage",
        "support": "console.manage",
    }
    visible = json.loads(run_script("""
var tabs = MENU_ITEMS.map(function (item) { return {view: item.view, hidden: false,
  getAttribute: function () { return item.view; }, setAttribute: function () {}}; });
document.querySelectorAll = function (selector) { return selector === '.tab[data-menu-view]' ? tabs.slice(2) : tabs; };
var sessions = %s, roles = {};
Object.keys(sessions).forEach(function (role) {
  S.consoleUser = sessions[role]; syncMenuVisibility();
  roles[role] = tabs.filter(function (tab) { return !tab.hidden; }).map(function (tab) { return tab.view; });
});
console.log(JSON.stringify(roles));
""" % json.dumps({role: console_session(role) for role in ROLE_PERMISSIONS})))
    for role, granted in ROLE_PERMISSIONS.items():
        expected = [view for view, _label in tabs if "*" in granted or permission_for_view[view] in granted]
        assert visible[role] == expected, role
        assert visible[role][:2] == ["cc", "work"]
    assert {"knowledge", "delivery", "lead", "review"} <= set(visible["team_lead"])
    assert "users" in visible["super_admin"] and "users" in visible["owner"]
    assert "machines" not in visible["team_lead"] and "sa" not in visible["lead"]


def test_dashboard_combines_work_roadmap_waiting_usage_quotas_tokens_and_cost():
    day = datetime.now(timezone.utc).date().isoformat()
    reset = int(datetime.now(timezone.utc).timestamp()) + 3600
    awareness = {"source": "reported", "is_estimate": False, "runs_24h": 20, "confidence": "high",
                 "rate_spread_ratio": .1, "usage_7d": {"tokens_in": 6000, "tokens_out": 1000,
                                                          "tokens": 7000, "cost_usd": .07},
                 "largest_run": {"ticket": "ALPHA-3", "tokens": 4000, "cost_usd": .04},
                 "history": [{"day": day, "tokens": 7000, "cost_usd": .07, "runs": 20}],
                 "windows": [{"name": "five_hour", "label": "Five hour", "source": "reported",
                              "used_pct": 40, "left_pct": 60, "used_tokens": 3000, "left_tokens": 4500,
                              "tokens": 3000, "cost_usd": .03,
                              "reset_at": datetime.fromtimestamp(reset, timezone.utc).isoformat(),
                              "reset_source": "reported", "burn_rates": {
                                  "last_hour": {"tokens_per_hour": 1000}, "last_day": {"tokens_per_hour": 800},
                                  "whole_window": {"tokens_per_hour": 700}},
                              "prediction": {"status": "comfortable", "confidence": "high",
                                             "remaining_at_reset_pct": 50}}]}
    state = {
        "projects": [{"key": "ALPHA", "repo": "jooservices/alpha", "closed": False}],
        "tickets": [
            {"key": "ALPHA-2", "project": "ALPHA", "type": "Epic", "summary": "Account roadmap",
             "status": "In Progress", "labels": []},
            {"key": "ALPHA-3", "project": "ALPHA", "type": "Story", "summary": "Building story",
             "status": "In Progress", "labels": [], "parent_key": "ALPHA-2", "run": None},
            {"key": "ALPHA-4", "project": "ALPHA", "type": "Bug", "summary": "Owner question",
             "status": "Needs owner", "labels": [], "parent_key": "ALPHA-2", "run": None},
            {"key": "ALPHA-5", "project": "ALPHA", "type": "Story", "summary": "Review this",
             "status": "Human review", "labels": [], "parent_key": "ALPHA-2", "run": None},
        ],
        "nodes": [{"name": "worker-1", "status": "online", "has_agent": True,
                   "accepting_work": True, "slots_total": 8, "sessions_busy": 0,
                   "metrics": {"mem_used": 1073741824, "mem_total": 8589934592, "cpus": 8}}],
        "accounts": [{"id": "codex-alpha", "provider": "codex", "node": "worker-1", "status": "idle",
                      "quota": {"windows": {"five_hour": {"utilization": 0.5, "resets_at": reset}}},
                      "quota_awareness": awareness}],
        "active_runs": [{"ticket": "ALPHA-3", "project": "ALPHA", "summary": "Building story",
                         "provider": "codex", "step": "dev", "elapsed_s": 120}],
        "recent_runs": [], "incidents": [], "events": [],
        "totals": {"runs_24h": 3, "done_24h": 2, "failed_24h": 1, "tokens_in_24h": 1200,
                   "tokens_out_24h": 800, "cost_24h": 0.12}, "versions": {},
    }
    analytics = [{"day": day, "account": "codex-alpha", "runs": 3, "done": 2, "merged": 0,
                  "rework": 0, "failed": 1, "tokens_in": 1200, "tokens_out": 800, "cost_usd": 0.12}]
    weekly = {"week_start": day, "trend": [], "finished_without_person": [], "needed_person": [],
              "repeated_failures": [], "quota_accounts": [{"id": "codex-alpha", "provider": "codex",
                                                               "quota_awareness": awareness}]}
    rendered = render_dashboard(state, analytics, weekly)
    assert rendered.count('class="dash-kpi"') == 4
    for text in ("Work", "Flow at a glance", "Projects", "What is being built", "Waiting for you",
                 "AI and machines", "AI usage", "codex-alpha", "Codex", "Next quota reset:",
                 "Quota forecast", "used 40% quota", "left 60%", "comfortable", "quota-history",
                 "Tokens · 24 h", "Cost · 24 h", "Tokens · 14 days", "Cost · 14 days",
                 "Needs owner", "Human review", "ALPHA-2", "provider-codex", "Master autonomy", "Daily budget",
                 "Weekly quota and usage", "Largest context run"):
        assert text in rendered
    assert 'data-act="epic-dashboard" data-key="ALPHA-2"' in rendered
    assert "aria-label=\"Open Work board filtered by epic ALPHA-2\"" in rendered
    assert 'id="dash-project-search"' in rendered and 'data-act="dashboard-sort" data-key="open"' in rendered

    machines = render_machines(state)
    assert "codex-alpha" in machines and "quota-history" in machines and "comfortable" in machines
    assert "used 40% quota" in machines and "left 60%" in machines
    assert "Largest context run" in machines and "Burn tokens/hour: 1h 1k, 24h 800, window 700" in machines


def test_account_without_provider_limits_is_labeled_as_an_estimate_in_the_console():
    estimate = {"id": "grok-estimate", "provider": "grok", "quota": {}, "quota_awareness": {
        "is_estimate": True, "confidence": "low", "runs_24h": 0,
        "usage_7d": {"tokens": 500, "cost_usd": .01}, "history": [], "largest_run": None,
        "windows": [{"label": "Estimated weekly", "source": "estimate", "used_pct": 20, "left_pct": 80,
                     "used_tokens": 500, "left_tokens": 2000, "tokens": 500, "cost_usd": .01,
                     "reset_at": "2026-10-26T00:00:00+00:00", "reset_source": "estimate",
                     "burn_rates": {}, "prediction": {"status": "comfortable", "confidence": "low"}}]}}
    rendered = run_script(f"console.log(qwin({json.dumps(estimate)}));")
    for label in ("Provider quota not reported", "Usage estimate", "Estimated weekly", "80%", "quota-history"):
        assert label in rendered


def test_search_and_work_board_picker_scale_star_and_filter_results():
    projects = [{"key": f"PRJ-{i:03}", "repo": f"jooservices/project-{i:03}"} for i in range(300)]
    prelude = STUBS + """
var head = {innerHTML: ''}, options = {innerHTML: '', hidden: true};
var input = {value: 'PRJ-2', setAttribute: function () {}};
document.activeElement = null;
document.querySelector = function (selector) {
  return selector === '#work-head' ? head : selector === '#project-options' ? options :
    selector === '#work-project' ? input : null;
};
"""
    driver = f"""
S.view = 'work'; S.project = 'PRJ-150'; S.projectQuery = 'PRJ-2'; S.projectPickerOpen = true;
S.starredProjects = ['PRJ-250', 'PRJ-200'];
S.data = {{projects: {json.dumps(projects)}, tickets: [
  {{key: 'PRJ-150-1', project: 'PRJ-150', type: 'Epic', summary: 'Project roadmap', status: 'To Do', labels: []}},
  {{key: 'PRJ-150-2', project: 'PRJ-150', type: 'Story', summary: 'Project ticket', status: 'To Do', labels: []}}
], accounts: [], nodes: [], active_runs: [], recent_runs: [],
  incidents: [], events: [], totals: {{}}, versions: {{}}}};
renderDHead(); renderProjectPickerOptions();
var pick = {{head: head.innerHTML, options: options.innerHTML}};
S.consoleUser = {{role: 'lead'}};
var results = quickSearchResults('PRJ-250');
console.log(JSON.stringify({{pick: pick, results: results}}));
"""
    out = json.loads(run_script(driver, prelude))
    assert 'id="work-project"' in out["pick"]["head"]
    assert 'id="project-options"' in out["pick"]["head"]
    assert 'id="epicSel"' in out["pick"]["head"] and 'id="ai-filter"' in out["pick"]["head"]
    assert 'id="q"' in out["pick"]["head"]
    assert 'data-act="project"' not in out["pick"]["head"]
    assert out["pick"]["options"].count('class="project-option"') <= 25
    assert "Starred projects" in out["pick"]["options"] and "PRJ-250" in out["pick"]["options"]
    assert any(row["kind"] == "project" and row["key"] == "PRJ-250" for row in out["results"])
    ticket_result = json.loads(run_script("""
S.data = {projects: [], tickets: [{key: 'ALPHA-3', project: 'ALPHA', summary: 'Searchable ticket'}]};
S.consoleUser = {role: 'lead'};
console.log(JSON.stringify(quickSearchResults('ALPHA-3')));
"""))
    assert any(row["kind"] == "ticket" and row["key"] == "ALPHA-3" for row in ticket_result)
    assert "aria-keyshortcuts=\"Control+K Meta+K\"" in html()
    assert "e.ctrlKey || e.metaKey" in html() and "openCommandSearch()" in html()
    chosen = json.loads(run_script("""
var selected = '';
var renderCount = 0;
setView = function (view) { selected = view; S.view = view; };
render = function () { renderCount += 1; };
S.consoleUser = {role: 'lead'}; S.view = 'work';
chooseSearchResult('project', 'PRJ-250');
console.log(JSON.stringify({view: selected, project: S.project, renders: renderCount}));
"""))
    assert chosen == {"view": "work", "project": "PRJ-250", "renders": 1}


def test_work_board_filters_epic_ai_state_and_ticket_text():
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the Work board")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    driver = r"""
var body = {innerHTML: ''};
document.querySelector = function (selector) { return selector === '#work-body' ? body : null; };
S.view = 'work'; S.project = 'ALPHA'; S.epic = 'ALPHA-2'; S.typeFilter = 'all'; S.aiFilter = 'working'; S.search = 'running needle';
S.data = {projects: [{key: 'ALPHA', repo: 'jooservices/alpha', work_minutes: {target: 30, warn: 45, hard: 60}}],
  tickets: [
    {key: 'ALPHA-2', project: 'ALPHA', type: 'Epic', summary: 'Epic', status: 'In Progress', labels: []},
    {key: 'ALPHA-3', project: 'ALPHA', type: 'Story', summary: 'Running needle', status: 'In Progress', labels: [],
      parent_key: 'ALPHA-2', run: {account: 'codex', step: 'dev', elapsed_s: 20}, blocked_by: []},
    {key: 'ALPHA-4', project: 'ALPHA', type: 'Bug', summary: 'Other text', status: 'In Progress', labels: [],
      parent_key: 'ALPHA-2', run: null, blocked_by: []},
    {key: 'BETA-4', project: 'BETA', type: 'Story', summary: 'Running needle', status: 'In Progress', labels: [],
      run: {account: 'codex', step: 'dev', elapsed_s: 20}, blocked_by: []}
  ], nodes: [], accounts: [], active_runs: [], recent_runs: [], incidents: [], events: [], totals: {}, versions: {}};
renderDBody(); console.log(body.innerHTML);
"""
    result = subprocess.run([node_js, "-"], input=STUBS + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'class="card" data-act="open-ticket" data-key="ALPHA-3"' in result.stdout
    assert 'class="card" data-act="open-ticket" data-key="ALPHA-4"' not in result.stdout
    assert 'class="card" data-act="open-ticket" data-key="BETA-4"' not in result.stdout


def test_warm_themes_keyboard_controls_phone_layout_and_old_blue_theme_removal():
    page = html()
    style = css()
    assert "--bg:#f5f1eb" in style and "--bg:#121110" in style
    assert ':root[data-theme="dark"]' in style and ':root:not([data-theme="light"])' in style
    assert "data-act=\"theme\"" in page and "aiflow4-theme" in page
    assert ":focus-visible" in style and "ArrowRight" in page and "ArrowLeft" in page
    assert "Control+K" in page and "Meta+K" in page and "Tab" in page
    assert "overflow-x:auto" in style and "@media (max-width:900px)" in style
    assert "@media (max-width:640px)" in style and "prefers-reduced-motion: reduce" in style
    tabs_rule = re.search(r"\.tabs\s*\{([^}]*)\}", style)
    tab_rule = re.search(r"\.tab\s*\{([^}]*)\}", style)
    assert tabs_rule and "min-width:0" in tabs_rule.group(1) and "overflow-x:auto" in tabs_rule.group(1)
    assert tab_rule and "flex:0 0 auto" in tab_rule.group(1) and "white-space:nowrap" in tab_rule.group(1)
    assert ".tabs{max-width:100%;width:100%}" in style
    for old_blue in ("#2453D6", "#88AAFF", "#0A0F1E", "#111933", "#7b6cff"):
        assert old_blue.lower() not in style.lower()
    assert ".reason-question_from_ai{background:var(--provider-claude)}" in style
    assert 'data-view="cc">Command Center</button>' not in page


def test_dashboard_epic_roadmap_opens_work_board_with_epic_filter():
    result = run_script("""
var selectedView = '';
setView = function (view) { selectedView = view; S.view = view; };
S.view = 'cc'; S.search = 'old search'; S.aiFilter = 'attention';
openEpicOnWorkBoard('ALPHA-2');
console.log(JSON.stringify({view: selectedView, epic: S.epic, search: S.search, aiFilter: S.aiFilter}));
""")
    assert json.loads(result) == {"view": "work", "epic": "ALPHA-2", "search": "", "aiFilter": "all"}
