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


def render_drawer(node: dict) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the console drawer")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    driver = f"""
var node = {json.dumps(node)};
S.data = {{nodes: [node], accounts: [], active_runs: [], recent_runs: [], projects: [], tickets: [],
          incidents: [], events: [], totals: {{}}, versions: {{}}}};
S.fetchedAt = Date.now();
console.log(drawerNode(node.name));
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
    s = client.get("/v1/state").json()
    assert s["version"] == VERSION
    assert s["env"] and "control_repo" in s and "versions" in s
    assert s["links"] == {"o2": "/o2/", "mail": "/mail/", "gatus": "/gatus/"}


def test_delivery_report_button_loads_markdown_into_copyable_drawer():
    page = html()
    assert 'data-act="open-report">Report</button>' in page
    assert "format=md" in page and "loadProjectReport(S.project, epic)" in page
    assert "function drawerReport()" in page and 'data-act="copy-report">Copy Markdown</button>' in page
    assert "else if (act === 'copy-report') copyReport()" in page
    assert "window.navigator.clipboard.writeText(text)" in page and "document.execCommand('copy')" in page


def test_gatus_link_is_configurable(monkeypatch):
    monkeypatch.setenv("AIFLOW4_GATUS_URL", "https://status.example/")
    assert Settings.from_env().gatus_url == "https://status.example/"
    monkeypatch.delenv("AIFLOW4_GATUS_URL")
    assert Settings.from_env().gatus_url == "/gatus/"
