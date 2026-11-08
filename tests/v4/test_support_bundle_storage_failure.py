"""Storage failure regression test for downloading support bundles."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aiflow.v4.control.api import Ctx
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session

SESSION_SECRET = "test-console-session-secret-at-least-32-bytes"


def sign_in(client: TestClient, ctx: Ctx, role: str = "owner", subject: str = "owner@example.test") -> None:
    """Authenticate a test client by issuing a signed console session cookie."""
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set(COOKIE_NAME, issue_session(subject, role, 2_000_000_000, SESSION_SECRET))


def test_download_support_bundle_answers_service_unavailable_when_storage_fails(
    client: TestClient, ctx: Ctx, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return HTTP 503 when the temporary bundle storage raises during download."""
    sign_in(client, ctx)

    def raise_storage_outage(key: str) -> str:
        """Raise a simulated runtime exception on storage access."""
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(ctx.leases.r, "get", raise_storage_outage)

    response = client.get("/v1/support/bundles/test-bundle-identifier/download")

    assert response.status_code == 503
    assert response.json()["detail"] == "temporary bundle storage is unavailable"
