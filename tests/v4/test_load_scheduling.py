"""Regression coverage for deterministic machine and shared-host load scheduling."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import load_policy, role_health
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session


PAGE = Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "ui" / "console.html"
ROOT = Path(__file__).resolve().parents[2]


def node_headers(client, name: str) -> dict[str, str]:
    joined = join_node(client, name)
    assert joined.status_code == 200, joined.text
    return {"Authorization": f"Bearer {joined.json()['node_token']}"}


def heartbeat(client, name: str, headers: dict[str, str], *, load1: float = 0.1, cpus: int = 4,
              mem_used: int = 20, disk_free_pct: float = 50, hostname: str | None = None,
              slots: int = 4):
    return client.post(f"/v1/nodes/{name}/heartbeat", headers=headers, json={
        "slots_total": slots,
        "metrics": {"hostname": hostname or f"{name}-host", "load1": load1, "cpus": cpus,
                    "mem_total": 100, "mem_used": mem_used, "disk_total": 100,
                    "disk_free": disk_free_pct, "disk_free_pct": disk_free_pct},
    })


def owner_session(client, ctx):
    secret = "test-console-session-secret-at-least-32-bytes"
    ctx.settings.console_session_secret = secret
    client.cookies.set(COOKIE_NAME, issue_session("owner@example.test", "owner", int(time.time()) + 3600, secret))


def prepare_account_capacity(ctx, account: str, sessions: int = 4):
    ctx._configured_repo().account(account).parallel = sessions


def test_default_machine_limits_and_owner_can_save_machine_and_host_limits(client, ctx):
    owner_session(client, ctx)
    headers = node_headers(client, "node1")
    assert heartbeat(client, "node1", headers, hostname="load-host").status_code == 200

    machine = next(row for row in client.get("/v1/state").json()["nodes"] if row["name"] == "node1")
    assert machine["load_limits"] == {
        "cpu_load_per_cpu": 0.85, "memory_used_pct": 85.0, "disk_free_pct": 10.0}
    assert machine["host_load_limits"] == {"cpu_load_per_cpu": 0.85}

    response = client.put("/v1/machines/node1/load-limits", json={
        "cpu_load_per_cpu": 0.9, "memory_used_pct": 90, "disk_free_pct": 8,
        "host_cpu_load_per_cpu": 0.75,
    })
    assert response.status_code == 200, response.text
    assert response.json()["load_limits"] == {
        "cpu_load_per_cpu": 0.9, "memory_used_pct": 90.0, "disk_free_pct": 8.0}
    state = client.get("/v1/state").json()
    machine = next(row for row in state["nodes"] if row["name"] == "node1")
    assert machine["load_limits"]["cpu_load_per_cpu"] == 0.9
    assert machine["host_load_limits"] == {"cpu_load_per_cpu": 0.75}
    assert client.put("/v1/machines/node1/load-limits", json={
        "cpu_load_per_cpu": 0, "memory_used_pct": 90, "disk_free_pct": 8,
        "host_cpu_load_per_cpu": 0.75,
    }).status_code == 422
    audit = ctx.db.one("SELECT actor,accepted FROM write_audit WHERE target='machine-load' AND action='limits'")
    assert audit == {"actor": "owner@example.test", "accepted": True}


@pytest.mark.parametrize(("metrics", "reason"), [
    ({"load1": 3.6, "cpus": 4, "mem_total": 100, "mem_used": 20, "disk_free_pct": 50}, "CPU load 0.90"),
    ({"load1": 0.1, "cpus": 4, "mem_total": 100, "mem_used": 85, "disk_free_pct": 50}, "memory use 85.0%"),
    ({"load1": 0.1, "cpus": 4, "mem_total": 100, "mem_used": 20, "disk_free_pct": 10}, "free disk 10.0%"),
])
def test_machine_hard_limits_stop_candidates_and_preserve_existing_runs(client, ctx, metrics, reason):
    headers = node_headers(client, "node1")
    prepare_account_capacity(ctx, "codex-01")
    add_ticket(ctx.db, "TESTWP-LOAD-KEEP")
    add_ticket(ctx.db, "TESTWP-LOAD-NEW")
    assert client.post("/v1/nodes/node1/heartbeat", headers=headers, json={
        "slots_total": 4, "metrics": {"hostname": "load-machine", **{
            "load1": 0.1, "cpus": 4, "mem_total": 100, "mem_used": 20, "disk_free_pct": 50}}}).status_code == 200
    current = client.post("/v1/work/claim", headers=headers,
                          json={"ticket": "TESTWP-LOAD-KEEP", "account": "codex-01"}).json()
    assert current["claimed"]

    overloaded = client.post("/v1/nodes/node1/heartbeat", headers=headers,
                             json={"slots_total": 4, "metrics": {"hostname": "load-machine", **metrics}})
    assert overloaded.status_code == 200
    offered = client.get("/v1/work/candidates", headers=headers, params={"account": "codex-01"}).json()
    assert offered["tickets"] == [] and reason in offered["reason"]
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (current["run_id"],))["status"] == "running"
    state = client.get("/v1/state").json()
    machine = next(row for row in state["nodes"] if row["name"] == "node1")
    assert machine["load_state"]["mode"] == "held"
    assert reason in machine["load_state"]["reason"]
    assert machine["load_state"]["measured_at"]
    assert ctx.db.one("SELECT count(*) AS n FROM events WHERE type='machine.load_throttled'")["n"] == 1


def test_held_machine_resumes_only_after_no_flapping_margin(client, ctx):
    headers = node_headers(client, "node1")
    prepare_account_capacity(ctx, "codex-01")
    add_ticket(ctx.db, "TESTWP-LOAD-EXISTING")
    add_ticket(ctx.db, "TESTWP-LOAD-RESUME")
    heartbeat(client, "node1", headers, hostname="resume-host", slots=4)
    active = client.post("/v1/work/claim", headers=headers,
                         json={"ticket": "TESTWP-LOAD-EXISTING", "account": "codex-01"}).json()
    assert active["claimed"]

    heartbeat(client, "node1", headers, hostname="resume-host", slots=4, load1=3.6)
    still_high = heartbeat(client, "node1", headers, hostname="resume-host", slots=4, load1=3.24)
    assert still_high.status_code == 200  # 0.81 remains inside the 0.05 recovery margin
    assert client.get("/v1/work/candidates", headers=headers,
                      params={"account": "codex-01"}).json()["tickets"] == []

    recovered = heartbeat(client, "node1", headers, hostname="resume-host", slots=4, load1=3.16)
    assert recovered.status_code == 200  # 0.79 clears the 0.80 recovery threshold
    offers = client.get("/v1/work/candidates", headers=headers, params={"account": "codex-01"}).json()
    assert "TESTWP-LOAD-RESUME" in {ticket["key"] for ticket in offers["tickets"]}
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (active["run_id"],))["status"] == "running"
    transitions = ctx.db.all("SELECT type FROM events WHERE type IN ('machine.load_throttled','machine.load_resumed') ORDER BY id")
    assert [event["type"] for event in transitions] == ["machine.load_throttled", "machine.load_resumed"]


def test_middle_band_caps_offers_to_half_of_machine_slots(client, ctx):
    headers = node_headers(client, "node1")
    prepare_account_capacity(ctx, "codex-01")
    for number in range(1, 5):
        add_ticket(ctx.db, f"TESTWP-LOAD-BAND-{number}")
    heartbeat(client, "node1", headers, hostname="band-host", slots=4, load1=2.4)

    first = client.get("/v1/work/candidates", headers=headers, params={"account": "codex-01"}).json()
    assert {ticket["key"] for ticket in first["tickets"]} == {
        "TESTWP-LOAD-BAND-1", "TESTWP-LOAD-BAND-2", "TESTWP-LOAD-BAND-3", "TESTWP-LOAD-BAND-4"}
    for _ in range(3):
        ticket = client.get("/v1/work/candidates", headers=headers, params={"account": "codex-01"}).json()["tickets"][0]
        claim = client.post("/v1/work/claim", headers=headers,
                            json={"ticket": ticket["key"], "account": "codex-01"}).json()
        assert claim["claimed"]
    capped = client.get("/v1/work/candidates", headers=headers, params={"account": "codex-01"}).json()
    assert capped["tickets"] == []
    assert "half" in capped["reason"]


@pytest.mark.parametrize("outcome", ["failed", "timeout"])
def test_overload_failure_is_excluded_and_immediately_offered_to_a_machine_with_room(client, ctx, outcome):
    node1 = node_headers(client, "node1")
    cc = node_headers(client, "cc")
    add_ticket(ctx.db, "TESTWP-LOAD-RETRY")
    heartbeat(client, "node1", node1, hostname="bad-load-host", slots=4)
    heartbeat(client, "cc", cc, hostname="roomy-host", slots=4)
    claim = client.post("/v1/work/claim", headers=node1,
                        json={"ticket": "TESTWP-LOAD-RETRY", "account": "codex-01"}).json()
    assert claim["claimed"]
    heartbeat(client, "node1", node1, hostname="bad-load-host", slots=4, load1=3.6)

    result = client.post(f"/v1/runs/{claim['run_id']}/result", headers=node1,
                         json={"fence": claim["fence"], "outcome": outcome, "summary": "worker stopped"})
    assert result.status_code == 200
    run = ctx.db.one("SELECT outcome,error_kind,details,summary FROM runs WHERE id=%s", (claim["run_id"],))
    assert run["outcome"] == "load_shed" and run["error_kind"] == "machine_load"
    assert run["details"]["load_shed"]["violations"][0]["scope"] == "machine"
    assert role_health.classify({"outcome": run["outcome"], "error_kind": run["error_kind"],
                                 "step": "dev", "summary": run["summary"]}).failure is None
    client.post(f"/v1/runs/{claim['run_id']}/release", headers=node1, json={"fence": claim["fence"]})
    retry = client.get("/v1/work/candidates", headers=cc, params={"account": "claude-01"}).json()
    assert "TESTWP-LOAD-RETRY" in {ticket["key"] for ticket in retry["tickets"]}


def test_shared_host_budget_is_enforced_across_machines_and_persisted_in_metrics(client, ctx):
    owner_session(client, ctx)
    node1 = node_headers(client, "node1")
    cc = node_headers(client, "cc")
    for ticket in ("TESTWP-HOST-1", "TESTWP-HOST-2"):
        add_ticket(ctx.db, ticket)
    heartbeat(client, "node1", node1, hostname="same-host", slots=4, load1=0.1)
    heartbeat(client, "cc", cc, hostname="same-host", slots=4, load1=0.1)
    saved = client.put("/v1/machines/node1/load-limits", json={
        "cpu_load_per_cpu": 0.85, "memory_used_pct": 85, "disk_free_pct": 10,
        "host_cpu_load_per_cpu": 0.3,
    })
    assert saved.status_code == 200, saved.text
    prepare_account_capacity(ctx, "codex-01")
    prepare_account_capacity(ctx, "claude-01")
    assert ctx.db.one("SELECT state->>'max_runs' AS max_runs FROM machine_host_load_limits"
                      " WHERE hostname='same-host'")["max_runs"] == "1"

    offered = client.get("/v1/work/candidates", headers=node1, params={"account": "codex-01"}).json()
    assert offered["tickets"]
    first = client.post("/v1/work/claim", headers=node1,
                        json={"ticket": offered["tickets"][0]["key"], "account": "codex-01"}).json()
    assert first["claimed"]
    blocked = client.get("/v1/work/candidates", headers=cc, params={"account": "claude-01"}).json()
    assert blocked["tickets"] == [] and "host capacity" in blocked["reason"]
    heartbeat(client, "cc", cc, hostname="same-host", slots=4, load1=1.6)
    host_events = ctx.db.all("SELECT type,data FROM events WHERE type='host.load_throttled'")
    assert host_events and host_events[0]["data"]["hostname"] == "same-host"
    assert host_events[0]["data"]["max_runs"] == 1


def test_machine_screen_and_openobserve_dashboard_surface_load_policy_and_telemetry():
    page = PAGE.read_text()
    assert "Load-aware offers" in page
    assert 'data-act="node-load-save"' in page
    assert "loadState.reason" in page and "loadState.measured_at" in page
    assert "hostLoadState.reason" in page
    import json
    dashboards = ROOT / "deploy" / "v4" / "o2" / "dashboards"
    dashboard = json.loads(next(dashboards.glob("*.json")).read_text())
    panels = [panel for tab in dashboard["tabs"] for panel in tab["panels"]]
    panel = next(panel for panel in panels if panel["title"] == "Throughput and load throttling")
    query = panel["queries"][0]
    assert "run.finished" in query["query"]
    assert "machine.load_throttled" in query["query"]
    assert "host.load_throttled" in query["query"]
    assert "cpu_load_per_cpu" in query["query"]
    assert {item["alias"] for item in query["fields"]["y"]} == {"events", "load_per_cpu"}


def test_load_policy_hysteresis_and_band_boundaries_are_deterministic():
    defaults = load_policy.evaluate_machine({"load1": 3.4, "cpus": 4}, None)
    assert defaults["mode"] == "held"  # equality reaches the configured ceiling
    high = load_policy.evaluate_machine({"load1": 3.6, "cpus": 4}, None)
    still_held = load_policy.evaluate_machine({"load1": 3.24, "cpus": 4}, None, high)
    clear = load_policy.evaluate_machine({"load1": 3.16, "cpus": 4}, None, still_held)
    assert still_held["mode"] == "held"
    assert clear["mode"] == "throttled"
    assert clear["throttle_ratio"] == 0.5
    below_band = load_policy.evaluate_machine({"load1": 2.36, "cpus": 4}, None)
    assert below_band["mode"] == "normal"
