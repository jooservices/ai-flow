"""AI account authorization links are single-use for a short window: an expired link is refused."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from conftest import join_node
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.features.fleet import routes_accounts

SECRET = "test-console-session-secret-at-least-32-bytes"
LINK = "https://auth.openai.com/codex/device"


def _owner(client, ctx) -> None:
    ctx.settings.console_session_secret = SECRET
    client.cookies.set(COOKIE_NAME, issue_session("owner@example.test", "owner", int(time.time()) + 3600, SECRET))


def _account_with_waiting_link(client, ctx, node: str):
    """Create a codex account and let its node report a provider link; return (account id, node auth, request id)."""
    _owner(client, ctx)
    node_auth = {"Authorization": f"Bearer {join_node(client, node, heartbeat=False).json()['node_token']}"}
    created = client.post(f"/v1/machines/{node}/accounts", json={
        "provider": "codex", "sessions": 1, "jobs": ["develop"]})
    assert created.status_code == 200, created.text
    account_id = created.json()["account"]
    request_id = ctx.db.one("SELECT sign_in_request_id FROM managed_ai_accounts WHERE id=%s",
                            (account_id,))["sign_in_request_id"]
    waiting = client.post(f"/v1/accounts/{account_id}/sign-in/status", headers=node_auth, json={
        "request_id": request_id, "status": "waiting", "sign_in_url": LINK, "code": "ABCD-EFGH"})
    assert waiting.status_code == 200, waiting.text
    return account_id, node_auth, request_id


def _expires_at(ctx, account_id: str) -> datetime:
    return ctx.db.one("SELECT sign_in_expires_at FROM managed_ai_accounts WHERE id=%s", (account_id,))["sign_in_expires_at"]


def test_provider_link_gets_a_fifteen_minute_window_when_the_node_reports_it(client, ctx):
    account_id, _node_auth, _request_id = _account_with_waiting_link(client, ctx, "node-link-window")

    remaining = _expires_at(ctx, account_id) - datetime.now(UTC)
    assert timedelta(minutes=14) < remaining <= timedelta(minutes=15)


def test_repeating_the_same_link_does_not_extend_its_window(client, ctx):
    account_id, node_auth, request_id = _account_with_waiting_link(client, ctx, "node-link-repeat")
    first = _expires_at(ctx, account_id)

    repeated = client.post(f"/v1/accounts/{account_id}/sign-in/status", headers=node_auth, json={
        "request_id": request_id, "status": "waiting", "sign_in_url": LINK, "code": "ABCD-EFGH"})

    assert repeated.status_code == 200, repeated.text
    assert _expires_at(ctx, account_id) == first


def test_expired_link_is_refused_on_read_and_on_a_new_waiting_report(client, ctx):
    account_id, node_auth, request_id = _account_with_waiting_link(client, ctx, "node-link-expired")
    ctx.db.run("UPDATE managed_ai_accounts SET sign_in_expires_at=now()-interval '1 minute' WHERE id=%s",
               (account_id,))

    details = client.get(f"/v1/accounts/{account_id}/sign-in")
    reported = client.post(f"/v1/accounts/{account_id}/sign-in/status", headers=node_auth, json={
        "request_id": request_id, "status": "waiting", "sign_in_url": "https://auth.openai.com/codex/other",
        "code": "WXYZ-1234"})
    failed = client.post(f"/v1/accounts/{account_id}/sign-in/status", headers=node_auth, json={
        "request_id": request_id, "status": "failed", "error": "provider link expired"})

    assert details.status_code == 410 and "start a new sign-in" in details.json()["detail"]
    assert reported.status_code == 409 and "expired" in reported.json()["detail"]
    assert failed.status_code == 200
    assert ctx.db.one("SELECT sign_in_url FROM managed_ai_accounts WHERE id=%s", (account_id,))["sign_in_url"] == LINK


def test_replaced_request_id_is_refused_after_a_fresh_sign_in(client, ctx):
    account_id, node_auth, old_request_id = _account_with_waiting_link(client, ctx, "node-link-replaced")
    assert client.post(f"/v1/accounts/{account_id}/sign-in").status_code == 200

    stale = client.post(f"/v1/accounts/{account_id}/sign-in/status", headers=node_auth, json={
        "request_id": old_request_id, "status": "waiting", "sign_in_url": LINK, "code": "ABCD-EFGH"})

    assert stale.status_code == 409 and "replaced" in stale.json()["detail"]


def test_link_expiry_check_treats_missing_and_future_expiry_as_live():
    now = datetime.now(UTC)

    assert routes_accounts._sign_in_link_expired(None) is False
    assert routes_accounts._sign_in_link_expired(now + timedelta(minutes=5)) is False
    assert routes_accounts._sign_in_link_expired(now - timedelta(seconds=1)) is True
