"""The can(permission) route dependency (JAIF-467)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiflow.v4.control.console_auth import ConsoleUser, issue_session
from aiflow.v4.control.platform.auth import can

from tests.v4.test_console_roles import SESSION_EXPIRY, SESSION_SECRET


def _app(ctx, login: bool) -> TestClient:
    ctx.settings.console_login = login
    ctx.settings.console_session_secret = SESSION_SECRET
    app = FastAPI()
    app.state.ctx = ctx

    @app.get("/dash")
    def dash(user: ConsoleUser | None = can("view.dashboard")):
        return {"subject": user.subject if user else None}

    @app.get("/admin")
    def admin(user: ConsoleUser | None = can("console.manage")):
        return {"subject": user.subject if user else None}

    return TestClient(app)


def test_with_console_sign_in_off_the_route_is_open_and_the_user_is_none(ctx):
    client = _app(ctx, login=False)

    assert client.get("/admin").json() == {"subject": None}
    client.cookies.set("aiflow4_session", issue_session("rev@example.test", "reviewer", SESSION_EXPIRY, SESSION_SECRET))
    assert client.get("/admin").json() == {"subject": "rev@example.test"}  # a session is still read when present


def test_with_sign_in_on_a_missing_session_is_401_and_a_role_without_the_permission_is_403(ctx):
    client = _app(ctx, login=True)

    assert client.get("/dash").status_code == 401
    client.cookies.set("aiflow4_session", issue_session("rev@example.test", "reviewer", SESSION_EXPIRY, SESSION_SECRET))
    assert client.get("/dash").json() == {"subject": "rev@example.test"}
    refused = client.get("/admin")
    assert (refused.status_code, refused.json()) == (403, {"detail": "not available for your role"})


def test_the_administrator_role_passes_every_permission(ctx):
    client = _app(ctx, login=True)
    client.cookies.set("aiflow4_session", issue_session("boss@example.test", "administrator", SESSION_EXPIRY, SESSION_SECRET))

    assert client.get("/admin").json() == {"subject": "boss@example.test"}
