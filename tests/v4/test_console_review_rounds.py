"""Console Dashboard coverage for review-round metrics."""
import json
import shutil
import subprocess

import pytest

from console_test_utils import console_script


STUBS = """
var document = {addEventListener: function () {}, documentElement: {setAttribute: function () {},
  classList: {add: function () {}, remove: function () {}}},
  getElementById: function () { return null; }, querySelector: function () { return null; },
  querySelectorAll: function () { return []; }};
var window = {matchMedia: function () { return {matches: false}; }, addEventListener: function () {}};
var location = {pathname: '/cc'};
var localStorage = {getItem: function () { return null; }, setItem: function () {}};
var fetch = function () { return new Promise(function () {}); };
var setInterval = function () {};
"""


def render_review_rounds_dashboard(state: dict, metrics: dict) -> str:
    node_js = shutil.which("node")
    if not node_js:
        pytest.skip("Node.js is needed to render the console Dashboard")
    script = console_script()
    driver = f"""
var dashboard = {{innerHTML: ''}};
document.querySelector = function (selector) {{ return selector === '#view-cc' ? dashboard : null; }};
S.data = {json.dumps(state)};
S.analytics = [];
S.dashboardUsage = [];
S.dashboardUsageError = '';
S.dashboardTab = 'work';
S.dashboardWaiting.keys = null;
S.project = null;
S.consoleUser = {{authenticated: true, user: 'owner', role: 'owner', permissions: []}};
S.dashboardQuota.rows = null;
S.dashboardQuota.user = S.consoleUser.user || '';
S.dashboardQuota.loadedAt = 0;
S.dashboardReviewRounds.data = {json.dumps(metrics)};
S.dashboardReviewRounds.loadedAt = Date.now();
S.autonomy.data = {{budget: {{used_minutes: 12, daily_budget_minutes: 90, remaining_minutes: 78}},
  report: {{week_start: '2026-10-19', trend: [], finished_without_person: [], needed_person: [], repeated_failures: []}}}};
S.esc.data = {{metrics: {{classes: [], labels: {{}}, person_classes: [], falling: false,
  this_week: {{total: 0, share_without_person_pct: 0}}, weeks: [], top_causes: []}},
  register: {{decisions: [], asked: [], amendments: []}}}};
renderDashboard();
console.log(dashboard.innerHTML);
"""
    result = subprocess.run([node_js, "-"], input=STUBS + script + driver + "\n})();\n",
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_dashboard_shows_review_round_target_and_findings_by_round():
    state = {"projects": [], "tickets": [], "nodes": [], "accounts": [], "active_runs": [],
             "recent_runs": [], "incidents": [], "events": [], "totals": {}, "versions": {}}
    metrics = {"target_rounds": 2, "merged_tickets": 1, "average_rounds": 2.0,
               "tickets": [{"key": "ALPHA-1", "rounds": 2,
                            "reviews": [{"round": 1, "findings": 2, "new_findings": 0},
                                        {"round": 2, "findings": 2, "new_findings": 1}]}]}

    rendered = render_review_rounds_dashboard(state, metrics)

    assert "AI review rounds" in rendered and "target average 2 rounds" in rendered
    assert "Average rounds" in rendered and "2.00 / 2 target" in rendered
    assert "ALPHA-1" in rendered and "New findings · rounds 2+" in rendered
    assert "<td>2</td><td>2</td><td>1</td>" in rendered
