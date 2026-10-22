"""Console login (TESTFLOW-108): users, scrypt passwords, session cookie, Super Administrator, the gate."""
import os
import stat

import pytest
from fastapi.testclient import TestClient

from conftest import join_node
from aiflow.v4 import cli
from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session, role_satisfies

SECRET = "s" * 40
EMAIL = "admin@example.test"


def make_client(ctx, gate=False):
    ctx.settings.console_session_secret = SECRET
    ctx.settings.console_login = gate
    app = create_app(ctx)
    app.state.sync_accounts()
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def seeded(ctx):
    password = login.seed_user(ctx.db, EMAIL, "super_admin", "Admin")
    assert password
    return password


def sign_in(client, password, email=EMAIL):
    return client.post("/v1/auth/login", json={"email": email, "password": password})


def test_passwords_are_salted_scrypt_hashes():
    a, b = login.hash_password("correct horse"), login.hash_password("correct horse")
    assert a != b and a.startswith("scrypt$") and "correct horse" not in a
    assert login.verify_password("correct horse", a) and not login.verify_password("wrong", a)
    assert not login.verify_password("x", "plain-text") and not login.verify_password("x", "bcrypt$1$2$3$4$5")
    assert len(login.generate_password()) >= 32


def test_seed_user_is_created_once_and_stores_only_a_hash(ctx):
    password = login.seed_user(ctx.db, "Admin@Example.Test", "super_admin")
    assert password and login.seed_user(ctx.db, EMAIL, "super_admin") is None
    row = ctx.db.one("SELECT email, role, password_hash FROM console_users")
    assert row["email"] == EMAIL and row["role"] == "super_admin" and password not in row["password_hash"]
    with pytest.raises(ValueError):
        login.seed_user(ctx.db, "not-an-email", "super_admin")
    with pytest.raises(ValueError):
        login.seed_user(ctx.db, "x@example.test", "nobody")


def test_sign_in_sets_a_signed_http_only_cookie_and_me_logout_work(ctx, seeded):
    client = make_client(ctx)
    r = sign_in(client, seeded)
    assert r.status_code == 200 and r.json()["role"] == "super_admin" and r.json()["role_label"] == "Super Administrator"
    cookie = r.headers["set-cookie"].lower()
    assert COOKIE_NAME in cookie and "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # public_url is http here
    me = client.get("/v1/auth/me")
    assert me.status_code == 200 and me.json()["email"] == EMAIL
    assert ctx.db.one("SELECT last_login_at FROM console_users")["last_login_at"] is not None
    assert client.post("/v1/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/v1/auth/me").status_code == 401


def test_secure_cookie_when_the_public_url_is_https(ctx, seeded):
    ctx.settings.public_url = "https://ai-cc.example.test"
    assert "secure" in sign_in(make_client(ctx), seeded).headers["set-cookie"].lower()


def test_wrong_email_and_wrong_password_look_the_same_and_five_failures_lock(ctx, seeded):
    client = make_client(ctx)
    unknown = sign_in(client, "whatever", "nobody@example.test")
    wrong = sign_in(client, "whatever")
    assert unknown.status_code == wrong.status_code == 401 and unknown.json() == wrong.json()
    for _ in range(login.MAX_FAILURES - 1):
        assert sign_in(client, "whatever").status_code == 401
    assert sign_in(client, seeded).status_code == 429     # locked, even with the right password
    again = login.reset_password(ctx.db, EMAIL)
    assert sign_in(client, again).status_code == 200
    assert not ctx.db.one("SELECT failed_attempts FROM console_users")["failed_attempts"]


def test_disabled_user_and_missing_fields_are_refused(ctx, seeded):
    client = make_client(ctx)
    ctx.db.run("UPDATE console_users SET disabled=true")
    assert sign_in(client, seeded).status_code == 401
    assert client.post("/v1/auth/login", json={"email": EMAIL}).status_code == 400
    assert client.post("/v1/auth/login", content=b"not json").status_code == 400


def test_login_audit_never_holds_the_password(ctx, seeded):
    client = make_client(ctx)
    sign_in(client, "bad-password-123")
    sign_in(client, seeded)
    rows = ctx.db.all("SELECT actor, accepted, result, payload::text AS payload FROM write_audit WHERE target='console'")
    assert [r["accepted"] for r in rows] == [False, True]
    assert all("bad-password-123" not in (r["result"] + r["payload"]) and seeded not in r["result"] + r["payload"] for r in rows)


def test_super_administrator_passes_every_role_check_and_lead_does_not():
    assert role_satisfies("super_admin", {"owner"}) and role_satisfies("owner", {"owner", "lead"})
    assert not role_satisfies("lead", {"owner"}) and not role_satisfies("unknown", {"owner"})


def test_owner_only_endpoints_accept_super_admin(ctx):
    client = make_client(ctx)
    exp = 4_102_444_800
    def get(role):
        client.cookies.set(COOKIE_NAME, issue_session("a@example.test", role, exp, SECRET))
        return client.get("/v1/integrations/github").status_code
    assert get("lead") == 403
    assert get("owner") == 200 and get("super_admin") == 200
    client.cookies.set(COOKIE_NAME, issue_session("a@example.test", "super_admin", exp, SECRET))
    assert client.get("/v1/console/session").json()["role"] == "super_admin"


def test_without_the_setting_nothing_is_gated(ctx):
    client = make_client(ctx, gate=False)
    assert client.get("/v1/state").status_code == 200


def test_gate_redirects_pages_and_rejects_api_calls_without_a_session(ctx, seeded):
    client = make_client(ctx, gate=True)
    r = client.get("/cc")
    assert r.status_code == 302 and r.headers["location"] == "/login?next=%2Fcc"
    assert client.get("/v1/state").status_code == 401
    assert client.get("/v1/state", headers={"Authorization": "Bearer not-a-token-route"}).status_code == 401
    for open_path in ("/healthz", "/login"):
        assert client.get(open_path).status_code == 200
    assert "Sign in" in client.get("/login").text
    assert sign_in(client, seeded).status_code == 200
    assert client.get("/v1/state").status_code == 200


def test_gate_lets_node_tokens_through_on_token_routes(ctx):
    client = make_client(ctx, gate=True)
    assert client.get("/v1/work/candidates", params={"account": "claude-01"}).status_code == 401
    joined = join_node(client, "node-gate")
    assert joined.status_code == 200      # join grants carry their own one-use token
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    assert client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers).status_code != 401


def test_next_is_only_a_path_on_this_site():
    assert login.safe_next("/delivery?x=1") == "/delivery?x=1"
    for bad in ("https://evil.example", "//evil.example", "/\\evil", "javascript:alert(1)", ""):
        assert login.safe_next(bad) == "/cc"


def test_cli_seed_writes_the_password_to_a_private_file_and_prints_nothing_secret(ctx, tmp_path, capsys, monkeypatch, test_database_url):
    from types import SimpleNamespace
    ctx.db.run("DELETE FROM console_users")
    args = SimpleNamespace(action="seed", email=EMAIL, role="super_admin", name="Admin", password_file=str(tmp_path / "pw"))
    settings = SimpleNamespace(database_url=test_database_url)
    assert cli._console_user(args, settings) == 0
    secret = (tmp_path / "pw").read_text().strip()
    assert len(secret) >= 32 and stat.S_IMODE(os.stat(tmp_path / "pw").st_mode) == 0o600
    out = capsys.readouterr().out
    assert secret not in out and "mode 0600" in out
    assert cli._console_user(args, settings) == 0
    assert "already exists" in capsys.readouterr().out
    assert (tmp_path / "pw").read_text().strip() == secret
