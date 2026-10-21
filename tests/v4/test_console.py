import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
