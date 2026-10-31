"""The knowledge write routes publish the events that write their existing audit rows."""
from __future__ import annotations

import ast
from pathlib import Path

from fastapi import HTTPException

from aiflow.v4.control import route_deps
from test_knowledge import save, sign_in


ROOT = Path(__file__).resolve().parents[2]


def assert_event_and_audit(db, event_type, *, target, action, ticket, actor, result, payload):
    events = db.all("SELECT type FROM domain_events WHERE type=%s", (event_type,))
    assert events == [{"type": event_type}]
    audit_payload = {**payload, "connection": "default"} if target == "aiflow-control" else payload
    rows = db.all("SELECT target, action, ticket, run_id, fence, actor, accepted, result, payload FROM write_audit "
                  "WHERE target=%s AND action=%s", (target, action))
    assert rows == [{"target": target, "action": action, "ticket": ticket, "run_id": "", "fence": None,
                     "actor": actor, "accepted": True, "result": result, "payload": audit_payload}]


def test_item_save_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    sign_in(client, ctx)
    response = client.post("/v1/knowledge/items", json={
        "scope": "general", "kind": "decision", "title": "Keep the rollout reversible",
        "content": "Use a small staged rollout.",
    })

    assert response.status_code == 200, response.text
    item_id = response.json()["id"]
    assert_event_and_audit(db, "knowledge.item_saved", target="knowledge", action="item_save",
                           ticket=str(item_id), actor="knowledge@example.test", result="saved",
                           payload={"kind": "decision", "project": "", "scope": "general"})
    event = db.one("SELECT aggregate,aggregate_id,payload FROM domain_events WHERE type='knowledge.item_saved'")
    assert event == {"aggregate": "knowledge_resource", "aggregate_id": str(item_id),
                     "payload": {"resource": str(item_id), "actor": "knowledge@example.test",
                                 "item_id": str(item_id), "kind": "decision", "project": "",
                                 "scope": "general"}}


def test_item_moderation_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    sign_in(client, ctx)
    saved = client.post("/v1/knowledge/items", json={
        "scope": "general", "kind": "decision", "title": "A moderation fixture",
        "content": "Approve this item for the moderation test.",
    })
    assert saved.status_code == 200, saved.text
    item_id = saved.json()["id"]

    response = client.post(f"/v1/knowledge/items/{item_id}/approve", json={})

    assert response.status_code == 200, response.text
    assert_event_and_audit(db, "knowledge.item_moderated", target="knowledge", action="item_approve",
                           ticket=str(item_id), actor="knowledge@example.test", result="updated",
                           payload={"item_id": item_id})
    event = db.one("SELECT aggregate,aggregate_id,payload FROM domain_events WHERE type='knowledge.item_moderated'")
    assert event == {"aggregate": "knowledge_resource", "aggregate_id": str(item_id),
                     "payload": {"resource": str(item_id), "actor": "knowledge@example.test",
                                 "item_id": item_id, "action": "approve"}}


def test_policy_proposal_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    sign_in(client, ctx)
    resources = client.get("/v1/knowledge/resources?include_archived=true").json()["resources"]
    current = next(row for row in resources if row["type"] == "policy" and row["id"] == "default")
    response = save(client, kind="policy", resource_id="default", title="Default policy",
                    content="version: 1\ncommands:\n  allow: ['git status']\n  deny: ['git push*']\n"
                            "files:\n  write_within: worktree\n", version=current["version"])

    assert response.status_code == 200, response.text
    data = response.json()
    pr = ctx.github.prs[-1]
    commit = f"commit{len(ctx.github.commits):04d}"
    expected_payload = {"type": "policy", "id": "default", "branch": pr["head"],
                        "pr": int(pr["number"]), "commit": commit}
    assert_event_and_audit(db, "knowledge.policy_proposed", target="aiflow-control", action="policy-pr",
                           ticket="", actor="knowledge@example.test", result=pr["html_url"],
                           payload=expected_payload)
    event = db.one("SELECT aggregate,aggregate_id,payload FROM domain_events WHERE type='knowledge.policy_proposed'")
    assert event == {"aggregate": "knowledge_resource", "aggregate_id": "default",
                     "payload": {"resource": "default", "actor": "knowledge@example.test", "kind": "policy",
                                 "resource_id": "default", "branch": pr["head"], "pr_number": int(pr["number"]),
                                 "pr_url": pr["html_url"], "commit": commit}}
    assert data["pr_url"] == pr["html_url"]


def test_resource_commit_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    sign_in(client, ctx)

    response = save(client, kind="knowledge", resource_id="jaif-478-audit-guide", title="Audit guide",
                    content="Keep the audit trail attached to the change.")

    assert response.status_code == 200, response.text
    resource = response.json()
    commit = resource["commit_sha"]
    expected_payload = {"type": "knowledge", "id": "jaif-478-audit-guide",
                        "version": resource["version"], "path": resource["path"]}
    assert_event_and_audit(db, "knowledge.resource_committed", target="aiflow-control", action="resource-commit",
                           ticket="", actor="knowledge@example.test", result=commit, payload=expected_payload)
    event = db.one("SELECT aggregate,aggregate_id,payload FROM domain_events WHERE type='knowledge.resource_committed'")
    assert event == {"aggregate": "knowledge_resource", "aggregate_id": "jaif-478-audit-guide",
                     "payload": {"resource": "jaif-478-audit-guide", "actor": "knowledge@example.test",
                                 "kind": "knowledge", "resource_id": "jaif-478-audit-guide",
                                 "resource_version": resource["version"], "path": resource["path"],
                                 "commit": commit}}


def test_policy_merge_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    sign_in(client, ctx)
    resources = client.get("/v1/knowledge/resources?include_archived=true").json()["resources"]
    current = next(row for row in resources if row["type"] == "policy" and row["id"] == "default")
    proposed = save(client, kind="policy", resource_id="default", title="Default policy",
                    content="version: 1\ncommands:\n  allow: ['git status']\n  deny: ['git push*']\n"
                            "files:\n  write_within: worktree\n", version=current["version"])
    assert proposed.status_code == 200, proposed.text
    proposal_id = client.get("/v1/knowledge/resources?include_archived=true").json()["proposals"][0]["proposal_id"]

    response = client.post(f"/v1/knowledge/proposals/{proposal_id}/approve", json={"approved_by": "Owner"},
                           headers={"Authorization": "Bearer owner-secret"})

    assert response.status_code == 200, response.text
    data = response.json()
    commit = str(data["sha"])
    resource = data["resource"]
    expected_payload = {"id": "default", "pr": 1, "version": resource["version"]}
    assert_event_and_audit(db, "knowledge.policy_merged", target="aiflow-control", action="policy-merge",
                           ticket="", actor="owner-token", result=commit, payload=expected_payload)
    event = db.one("SELECT aggregate,aggregate_id,payload FROM domain_events WHERE type='knowledge.policy_merged'")
    assert event == {"aggregate": "knowledge_resource", "aggregate_id": "default",
                     "payload": {"resource": "default", "actor": "owner-token", "resource_id": "default",
                                 "pr_number": 1, "resource_version": resource["version"], "commit": commit}}


def test_policy_proposal_rolls_back_the_proposal_row_with_the_audit(client, ctx, db, monkeypatch):
    sign_in(client, ctx)
    resources = client.get("/v1/knowledge/resources?include_archived=true").json()["resources"]
    current = next(row for row in resources if row["type"] == "policy" and row["id"] == "default")

    def fail_audit(*args, **kwargs):
        raise HTTPException(500, "forced audit failure")

    monkeypatch.setattr(route_deps, "audit", fail_audit)
    response = save(client, kind="policy", resource_id="default", title="Default policy",
                    content="version: 1\ncommands:\n  allow: ['git status']\n  deny: ['git push*']\n"
                            "files:\n  write_within: worktree\n", version=current["version"])

    assert response.status_code == 500
    assert db.one("SELECT count(*) AS n FROM managed_resource_proposals WHERE resource_type='policy' "
                  "AND resource_id='default'")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE type='knowledge.policy_proposed'")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM write_audit WHERE target='aiflow-control' AND action='policy-pr'")["n"] == 0


def test_the_knowledge_routes_no_longer_call_audit():
    routes = ROOT / "aiflow/v4/control/features/knowledge/routes.py"
    module = ast.parse(routes.read_text(encoding="utf-8"))

    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "audit"
                   for node in ast.walk(module))
