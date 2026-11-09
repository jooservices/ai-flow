"""AC1/AC2 of JAIF-595 (control and Dashboard): CPU overview is open and the console renders the split."""
from __future__ import annotations

from aiflow.v4.control import cpu_overview
from aiflow.v4.control.ui import build_console_page
from aiflow.v4.node import agent
from conftest import join_node


def test_the_overview_splits_the_cpu_of_a_machine_into_four_groups():
    row = {"name": "node1", "role": "worker", "age": 12.0, "metrics": {
        "cpu_pct": 71.26, "cpus": 24,
        "cpu_breakdown": {"ai_tools": 2.04, "tests": 44.0, "browsers": 6.5, "databases": 9.9}}}
    machine = cpu_overview.machine_cpu(row)
    assert machine == {
        "node": "node1", "role": "worker", "online": True, "cpus": 24, "cpu_pct": 71.3,
        "breakdown": {"ai_tools": 2.0, "tests": 44.0, "browsers": 6.5, "databases": 9.9},
    }


def test_the_overview_tolerates_a_machine_that_has_not_reported_a_breakdown():
    machine = cpu_overview.machine_cpu({"name": "old", "metrics": None, "age": 600})
    assert machine["online"] is False and machine["cpu_pct"] is None
    assert machine["breakdown"] == {"ai_tools": None, "tests": None, "browsers": None, "databases": None}
    hostile = cpu_overview.machine_cpu({"name": "x", "age": 1, "metrics": {
        "cpu_breakdown": {"tests": "lots", "ai_tools": float("nan"), "browsers": 900, "databases": -4}}})
    assert hostile["breakdown"] == {"ai_tools": None, "tests": None, "browsers": 100.0, "databases": 0.0}


def node_headers(client, name):
    joined = join_node(client, name)
    assert joined.status_code == 200, joined.text
    return {"Authorization": f"Bearer {joined.json()['node_token']}"}


def beat(client, name, headers, **metrics):
    return client.post(f"/v1/nodes/{name}/heartbeat", headers=headers, json={"slots_total": 2, "metrics": {
        "hostname": f"{name}-host", "load1": 0.1, "cpus": 8, "mem_total": 100, "mem_used": 20,
        "disk_total": 100 * 1024 ** 3, "disk_free": 50 * 1024 ** 3, "disk_free_pct": 50, **metrics}})


def test_anyone_can_read_the_overview_without_signing_in(client, ctx):
    """AC1: Dashboard data is visible without signing in."""
    ctx.settings.console_login = True
    headers = node_headers(client, "node1")
    breakdown = {"tests": 30, "ai_tools": 1, "browsers": 5, "databases": 8}
    assert beat(client, "node1", headers, cpu_pct=50, cpu_breakdown=breakdown).status_code == 200
    client.cookies.clear()
    response = client.get("/v1/cpu-overview")
    assert response.status_code == 200
    node = next(item for item in response.json()["nodes"] if item["node"] == "node1")
    assert node["breakdown"] == {"ai_tools": 1.0, "tests": 30.0, "browsers": 5.0, "databases": 8.0}
    assert node["online"] is True and node["cpu_pct"] == 50.0
    assert set(node) == {"node", "role", "online", "cpus", "cpu_pct", "breakdown"}


def test_dashboard_cpu_panel_is_injected_and_fetches_the_overview_route():
    """AC2: the Dashboard rendering includes the four-group CPU panel."""
    page = build_console_page()
    assert "function dashboardCpuPanel()" in page
    assert 'data-block="cpu-use"' in page
    assert "CPU use by machine" in page
    assert "AI tools" in page and "Tests" in page and "Browsers" in page and "Databases" in page
    assert "fetch('/v1/cpu-overview'" in page
    assert "loadDashboardCpu(false)" in page
    assert "visible without signing in" in page
    html = page.split("function dashboardCpuPanel", 1)[0]
    assert 'data-block="cpu-use"' not in html  # panel is injected, not baked into console.html


def test_node_and_control_agree_on_the_four_group_names():
    assert agent.cpu_breakdown.GROUPS == cpu_overview.GROUPS
