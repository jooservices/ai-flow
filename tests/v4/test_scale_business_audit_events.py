"""The scale proof and business routes publish the events that write their audit rows."""
from __future__ import annotations

import ast
from pathlib import Path

from fastapi import HTTPException

from aiflow.v4.control import route_deps
from test_business import epic
from test_scale_proof import OWNER, synthetic
from test_stakeholder import successful_change


ROOT = Path(__file__).resolve().parents[2]


def assert_event_and_audit(db, event_type, *, target, action, ticket, actor, result, payload):
    assert db.all("SELECT type FROM domain_events WHERE type=%s", (event_type,)) == [{"type": event_type}]
    rows = db.all("SELECT target, action, ticket, run_id, fence, actor, accepted, result, payload FROM write_audit "
                  "WHERE target=%s AND action=%s", (target, action))
    assert rows == [{"target": target, "action": action, "ticket": ticket, "run_id": "", "fence": None,
                     "actor": actor, "accepted": True, "result": result, "payload": payload}]


def test_scale_proof_result_publishes_one_event_and_writes_the_same_audit_row(client, db):
    response = client.post("/v1/scale-proof/results", json={"release": "v4.7.0", "report": synthetic()},
                           headers=OWNER)

    assert response.status_code == 200, response.text
    result = response.json()
    assert_event_and_audit(db, "scale_proof.result_filed", target="aiflow-control", action="scale-proof-result",
                           ticket="", actor="owner-token", result="passed" if result["passed"] else "failed",
                           payload={"run_id": result["run_id"], "release": result["release"]})


def test_change_request_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    epic(ctx.db, "TESTWP-1")
    description = "Refunds should take one day."

    response = client.post("/v1/business/TESTWP/change-requests", json={
        "title": "Faster refunds", "description": description, "epic": "TESTWP-1",
    })

    assert response.status_code == 200, response.text
    created = response.json()
    assert_event_and_audit(db, "business.change_request_filed", target="jira", action="create",
                           ticket=created["key"], actor="business-console", result=created["status"],
                           payload={"project": "TESTWP", "type": "Story", "labels": ["change-request"],
                                    "epic": "TESTWP-1", "chars": len(description)})


def test_stakeholder_acceptance_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    successful_change(ctx, epic="TESTWP-2")

    response = client.post("/v1/stakeholder/TESTWP/verdict", json={
        "change_key": "TESTWP-20", "target_key": "TESTWP-2", "environment": "develop", "verdict": "expected",
        "sender": "Mai Nguyen", "release_commit": "abc123",
    })

    assert response.status_code == 200, response.text
    assert_event_and_audit(db, "business.stakeholder_accepted", target="jira", action="stakeholder_acceptance",
                           ticket="TESTWP-2", actor="Mai Nguyen", result="1",
                           payload={"project": "TESTWP", "change": "TESTWP-20", "environment": "develop"})


def test_stakeholder_bug_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    successful_change(ctx)

    response = client.post("/v1/stakeholder/TESTWP/verdict", json={
        "change_key": "TESTWP-20", "environment": "production", "verdict": "not_expected",
        "sender": "Mai Nguyen", "expected": "The new total appears.", "actual": "The old total remains.",
        "screenshot_data": "data:image/png;base64,cG5n", "screenshot_filename": "uat.png",
    })

    assert response.status_code == 200, response.text
    issue_key = response.json()["key"]
    assert_event_and_audit(db, "business.stakeholder_bug_filed", target="jira", action="stakeholder_bug",
                           ticket=issue_key, actor="Mai Nguyen", result="Intake",
                           payload={"project": "TESTWP", "change": "TESTWP-20", "environment": "production",
                                    "screenshot": True})
    event = db.one("SELECT payload FROM domain_events WHERE type='business.stakeholder_bug_filed'")
    assert event["payload"]["screenshot"] is True
    assert "screenshot_data" not in event["payload"]


def test_stakeholder_bug_rolls_back_the_ticket_row_with_the_audit(client, ctx, db, monkeypatch):
    successful_change(ctx)

    def fail_audit(*args, **kwargs):
        raise HTTPException(500, "forced audit failure")

    monkeypatch.setattr(route_deps, "audit", fail_audit)
    response = client.post("/v1/stakeholder/TESTWP/verdict", json={
        "change_key": "TESTWP-20", "environment": "develop", "verdict": "not_expected",
        "sender": "Mai Nguyen", "expected": "A", "actual": "B",
    })

    assert response.status_code == 500
    assert len(ctx.jira.created) == 1
    issue_key = ctx.jira.created[0]["key"]
    assert db.one("SELECT key FROM tickets WHERE key=%s", (issue_key,)) is None
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE type='business.stakeholder_bug_filed'")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM write_audit WHERE target='jira' AND action='stakeholder_bug'")["n"] == 0


def test_the_two_route_modules_no_longer_call_audit():
    for relative in ("aiflow/v4/control/features/scale_proof/routes.py",
                     "aiflow/v4/control/features/business/routes.py"):
        module = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "audit"
                       for node in ast.walk(module)), relative
