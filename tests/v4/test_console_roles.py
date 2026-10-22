"""Console roles expose their UI permissions and enforce them at API boundaries."""
from __future__ import annotations

from aiflow.v4.control.console_auth import ROLE_PERMISSIONS, issue_session


SESSION_SECRET = "test-console-role-secret-with-at-least-32-bytes"


def sign_in(client, ctx, role: str, subject: str = "person@example.test"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session(subject, role, 2_000_000_000, SESSION_SECRET))


def test_super_admin_session_returns_authoritative_role_permissions(client, ctx):
    sign_in(client, ctx, "super_admin", "admin@example.test")

    response = client.get("/v1/console/session")
    assert response.status_code == 200
    assert response.json() == {
        "authenticated": True,
        "user": "admin@example.test",
        "role": "super_admin",
        "permissions": sorted(ROLE_PERMISSIONS["super_admin"]),
    }


def test_lead_can_read_knowledge_but_cannot_write_it(client, ctx):
    sign_in(client, ctx, "lead", "lead@example.test")

    assert client.get("/v1/knowledge/resources").status_code == 200
    refused = client.post("/v1/knowledge/resources", json={
        "type": "unknown", "id": "example", "title": "Example", "content": "text",
        "actor": "spoofed actor", "archived": False,
    })
    assert refused.status_code == 403


def test_super_admin_knowledge_write_reaches_resource_validation(client, ctx):
    sign_in(client, ctx, "super_admin", "admin@example.test")

    response = client.post("/v1/knowledge/resources", json={
        "type": "unknown", "id": "example", "title": "Example", "content": "text",
        "actor": "spoofed actor", "archived": False,
    })
    assert response.status_code == 400
    assert response.json()["detail"] == "unknown resource type"


def test_policy_approval_keeps_the_owner_bearer_token_path(client, ctx):
    response = client.post("/v1/knowledge/proposals/1/approve", json={"approved_by": "owner"},
                           headers={"Authorization": "Bearer owner-secret"})
    assert response.status_code == 404
