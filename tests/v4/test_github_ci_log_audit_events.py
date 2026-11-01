"""Audit event coverage for the GitHub CI log route."""
from __future__ import annotations

import json
import uuid

from conftest import add_ticket, join_node
from aiflow.v4.control import route_deps


def test_ci_log_read_records_domain_event_and_write_audit(client, ctx, monkeypatch):
    joined = join_node(client, "cc")
    assert joined.status_code == 200
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    add_ticket(ctx.db, "TESTWP-9")
    prior_dev = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,pr_url,branch,head_sha,details)"
               " VALUES (%s,'TESTWP-9','TESTWP','dev','claude-01','claude','cc',1,'finished','done',"
               "'https://github.com/jooservices/test-aiflow-wp/pull/9','TESTWP-9-x','sha-TESTWP-9-x',%s::jsonb)",
               (prior_dev, json.dumps({"repository": "jooservices/test-aiflow-wp"})))
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-9", "account": "claude-01"},
                        headers=headers).json()

    def failing_job_log(repo, sha, lines=200):
        return {"found": True, "job": "Test (v4)", "workflow": "CI", "conclusion": "failure",
                "url": "https://github.com/jooservices/test-aiflow-wp/actions/runs/1/job/2",
                "failed_steps": ["Run tests"], "lines": 2,
                "tail": "FAILED tests/v4/test_x.py::test_y\nAssertionError: 1 != 2"}

    ctx.github.pr_data = {9: {"number": 9, "head": {"ref": "TESTWP-9-x", "sha": "sha-TESTWP-9-x",
                              "repo": {"full_name": "jooservices/test-aiflow-wp"}}}}
    monkeypatch.setattr(ctx.github, "failing_job_log", failing_job_log, raising=False)
    route_deps.github_connection_used.set("")
    got = client.get("/v1/github/ci-log", params={"run_id": claim["run_id"], "fence": claim["fence"],
                                                   "branch": "TESTWP-9-x", "lines": 50}, headers=headers)

    assert got.status_code == 200, got.text
    body = got.json()
    assert body["found"] and body["job"] == "Test (v4)" and "AssertionError: 1 != 2" in body["tail"]
    assert ctx.db.all("SELECT type,aggregate,aggregate_id,payload FROM domain_events "
                      "WHERE type='github.ci_log_read' ORDER BY seq") == [
        {"type": "github.ci_log_read", "aggregate": "run", "aggregate_id": claim["run_id"],
         "payload": {"run_id": claim["run_id"], "fence": claim["fence"], "ticket": "TESTWP-9",
                     "actor": "claude-01", "job": "Test (v4)", "sha": "sha-TESTWP-9", "lines": 50}}]
    assert ctx.db.all("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                      "WHERE target='github' AND action='ci-log' AND accepted=true") == [
        {"target": "github", "action": "ci-log", "ticket": "TESTWP-9", "run_id": claim["run_id"],
         "fence": claim["fence"], "actor": "claude-01", "accepted": True, "result": "Test (v4)",
         "payload": {"sha": "sha-TESTWP-9", "lines": 50, "connection": "default"}}]
