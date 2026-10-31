from __future__ import annotations

import ast
import uuid
from pathlib import Path

from conftest import join_node
from aiflow.v4.control.incidents import flow_bug_candidates, open_incident
from aiflow.v4.core.settings import Settings


ROOT = Path(__file__).parents[2]


def failed_run(ctx, project, ticket, summary="Git checkout failed while preparing review", minutes_ago=0):
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,error_kind,summary)"
               " VALUES (%s,%s,%s,'verify','codex-01','codex','node1',1,'finished','failed','infra',%s)",
               (run_id, ticket, project, summary))
    if minutes_ago:
        ctx.db.run("UPDATE runs SET started_at=now()-(%s * interval '1 minute'),"
                   " ended_at=now()-(%s * interval '1 minute') WHERE id=%s",
                   (minutes_ago, minutes_ago, run_id))
    return run_id


def ready(client, name, account):
    joined = join_node(client, name)
    assert joined.status_code == 200
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    response = client.post(f"/v1/nodes/{name}/heartbeat", json={
        "workers": [{"account": account, "state": "idle"}], "slots_total": 2,
        "metrics": {"hostname": name, "load1": 0.1, "cpus": 4, "mem_total": 100, "mem_used": 20,
                    "disk_total": 100 * 1024 ** 3, "disk_free": 20 * 1024 ** 3, "disk_free_pct": 20},
    }, headers=headers)
    assert response.status_code == 200
    return headers


def test_candidates_follow_the_maintenance_project(ctx):
    ctx.settings.maintenance_project = "OPSX"
    for project in ("OPSX", "TESTFLOW"):
        for ticket in (f"{project}-1", f"{project}-2", f"{project}-1"):
            failed_run(ctx, project, ticket)

    candidates = flow_bug_candidates(ctx)

    assert len(candidates) == 1
    assert candidates[0]["tickets"] == ["OPSX-1", "OPSX-2"]


def test_ticket_stuck_incidents_are_matched_by_literal_prefix(ctx):
    ctx.settings.maintenance_project = "OPS_X"
    nonmatching_run = failed_run(ctx, "OPS_X", "OPSAX-1", minutes_ago=45)
    matching_run = failed_run(ctx, "OPS_X", "OPS_X-7", minutes_ago=45)
    open_incident(ctx, "ticket_stuck", "OPSAX-1", "warning", "Checkout is stuck", "Repeated failures")
    open_incident(ctx, "ticket_stuck", "OPS_X-7", "warning", "Checkout is stuck", "Repeated failures")
    ctx.db.run("UPDATE incidents SET opened_at=now()-interval '31 minutes'"
               " WHERE kind='ticket_stuck' AND subject IN (%s,%s)", ("OPSAX-1", "OPS_X-7"))

    candidates = flow_bug_candidates(ctx)

    assert len(candidates) == 1
    assert candidates[0]["tickets"] == ["OPS_X-7"]
    candidate_run_ids = {run["id"] for run in candidates[0]["runs"]}
    assert matching_run in candidate_run_ids and nonmatching_run not in candidate_run_ids


def test_flow_bug_is_filed_in_the_maintenance_project(client, ctx):
    ctx.settings.maintenance_project = "OPSX"
    headers = ready(client, "cc", "claude-01")
    claimed = client.post("/v1/work/claim", json={
        "ticket": "master:global", "account": "claude-01", "step": "master",
    }, headers=headers)
    assert claimed.status_code == 200 and claimed.json()["claimed"]
    run = claimed.json()
    for ticket in ("OPSX-1", "OPSX-1", "OPSX-2"):
        failed_run(ctx, "OPSX", ticket)

    scan = client.post("/v1/master/flow-bugs", json={"run_id": run["run_id"], "fence": run["fence"]},
                       headers=headers)

    assert scan.status_code == 200 and scan.json()["reports"][0]["action"] == "created"
    created = ctx.jira.created[-1]
    assert created["project"] == "OPSX"
    assert ctx.db.one("SELECT project FROM tickets WHERE key=%s", (created["key"],))["project"] == "OPSX"
    event = ctx.db.one("SELECT project FROM events WHERE type='flow_bug.created' AND ticket=%s", (created["key"],))
    assert event["project"] == "OPSX"


def test_the_default_maintenance_project_is_the_ai_flow_project(monkeypatch):
    monkeypatch.delenv("AIFLOW4_MAINTENANCE_PROJECT", raising=False)
    assert Settings().maintenance_project == "JAIF"

    monkeypatch.setenv("AIFLOW4_MAINTENANCE_PROJECT", "OPSX")
    assert Settings.from_env().maintenance_project == "OPSX"


def test_flow_bug_code_prompt_and_docs_name_no_legacy_project_key():
    files = (
        "aiflow/v4/control/incidents.py",
        "aiflow/v4/control/features/runs/routes.py",
        "prompts/v4/master.md",
        "docs/v4/flow-bugs.md",
        "docs/v4/master.md",
    )
    legacy_keys = ("TESTFLOW", "TESTWP", "JOOWT", "JOOS")
    for relative_path in files:
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert not any(key in content for key in legacy_keys), relative_path

    app_helpers = (ROOT / "aiflow/v4/control/app_helpers.py").read_text(encoding="utf-8")
    tree = ast.parse(app_helpers)
    functions = {
        node.name: ast.get_source_segment(app_helpers, node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"create_flow_bug", "process_flow_bugs"}
    }
    assert set(functions) == {"create_flow_bug", "process_flow_bugs"}
    for source in functions.values():
        assert source is not None
        assert not any(key in source for key in legacy_keys)
