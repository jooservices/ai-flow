"""The dashboard read routes are served from their feature module (JAIF-401)."""
from __future__ import annotations

import uuid

from aiflow.v4.control import api
from aiflow.v4.control.incidents import flow_error_pattern
from aiflow.v4.control.routes import dashboard

PATHS = {("GET", "/v1/dashboard/waiting"), ("GET", "/v1/dashboard/disagreements"),
         ("GET", "/v1/dashboard/quota"), ("GET", "/v1/dashboard/review-rounds"),
         ("GET", "/v1/dashboard/flow-failures"), ("GET", "/v1/dashboard/yield"),
         ("GET", "/v1/dashboard/pipeline-failures"),
         ("GET", "/v1/dashboard/assumptions"), ("GET", "/v1/dashboard/timeouts")}


def test_the_module_defines_exactly_its_dashboard_routes():
    assert {(m, r.path) for r in dashboard.router.routes for m in r.methods if m != "HEAD"} == PATHS


def test_the_app_serves_them_from_the_module(ctx):
    served = {}
    for route in api.create_app(ctx).routes:
        inner = getattr(route, "original_router", None)
        for r in (inner.routes if inner else [route]):
            for method in getattr(r, "methods", ()) or ():
                if (method, getattr(r, "path", "")) in PATHS:
                    served[(method, r.path)] = r.endpoint.__module__
    assert set(served) == PATHS and set(served.values()) == {"aiflow.v4.control.routes.dashboard"}


def test_the_review_queue_is_reachable_for_routers_through_the_app_state(ctx):
    assert api.create_app(ctx).state.review_queue is not None


def test_the_routes_answer(client):
    for _, path in sorted(PATHS):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)


def test_flow_failure_dashboard_summarizes_signatures_and_bug_state(client, ctx):
    summary = "git fetch failed for TESTWP-200: branch unavailable"
    run_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for run_id, ticket in zip(run_ids, ("TESTWP-200", "TESTWP-201"), strict=True):
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
                   "error_kind,summary,tokens_in,tokens_out,cost_usd) VALUES (%s,%s,'TESTWP','verify','codex-01',"
                   "'codex','node1',1,'finished','failed','infra',%s,2,3,0.25)", (run_id, ticket, summary))
    signature = flow_error_pattern("infra", summary, "verify", "failed")[0]
    ctx.db.run("INSERT INTO tickets (key,project,status,type,labels,summary)"
               " VALUES ('JAIF-999','JAIF','In Progress','Bug',%s,'Repeated failure')", (["flow-bug"],))
    ctx.db.run("INSERT INTO flow_bug_tracker (signature,ticket_key,regression_count) VALUES (%s,'JAIF-999',2)",
               (signature,))

    response = client.get("/v1/dashboard/flow-failures")

    assert response.status_code == 200
    data = response.json()
    assert data["signatures"][0]["count"] == 2
    assert data["signatures"][0]["tickets"] == ["TESTWP-200", "TESTWP-201"]
    assert data["signatures"][0]["tokens"] == 10
    assert data["signatures"][0]["bug"] == {"ticket": "JAIF-999", "state": "In Progress", "regressions": 2}
    assert data["runs_with_bug"] == 2 and data["bugged_run_share"] > 0
