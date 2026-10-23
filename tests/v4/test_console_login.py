"""Console login (JAIF-109): users, scrypt passwords, session cookie, rate limits, Super Administrator, the gate."""
import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from conftest import join_node
from aiflow.v4 import cli
from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
from aiflow.v4.control import console_auth, licensing
from aiflow.v4.control.console_auth import COOKIE_NAME, has_permission, issue_session, role_satisfies
from aiflow.v4.control.db import J
from aiflow.v4.core.settings import Settings

SECRET = "s" * 40
EMAIL = "admin@example.test"
EXP = 4_102_444_800


def make_client(ctx, gate=False):
    ctx.settings.console_session_secret = SECRET
    ctx.settings.console_login = gate
    app = create_app(ctx)
    app.state.sync_accounts()
    return TestClient(app, follow_redirects=False, client=("127.0.0.1", 50000))


@pytest.fixture
def seeded(ctx):
    password = login.seed_user(ctx.db, EMAIL, "super_admin", "Admin")
    assert password
    return password


def sign_in(client, password, email=EMAIL, ip=""):
    headers = {"CF-Connecting-IP": ip} if ip else {}
    return client.post("/v1/auth/login", json={"email": email, "password": password}, headers=headers)


def user_row(ctx):
    return ctx.db.one("SELECT failed_attempts, locked_until, last_login_at FROM console_users WHERE email=%s", (EMAIL,))


# --- AC2: passwords ------------------------------------------------------------------------------------------------
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


def test_users_screen_creates_accounts_and_shows_the_licence_limit_refusal(ctx, seeded):
    client = make_client(ctx, gate=True)
    assert sign_in(client, seeded).status_code == 200
    limits = {plan: dict(values) for plan, values in licensing.PLAN_DEFAULTS.items()}
    limits["free"]["users"] = 2
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(limits),))

    listed = client.get("/v1/auth/users")
    assert listed.status_code == 200 and listed.json()["count"] == 1 and listed.json()["limit"] == 2
    created = client.post("/v1/auth/users", json={"email": "new@example.test", "display_name": "New User",
                                                  "role": "lead"})
    assert created.status_code == 201
    result = created.json()
    assert result["email"] == "new@example.test" and result["role"] == "lead"
    assert result["initial_password"] and result["message"]
    stored = ctx.db.one("SELECT password_hash FROM console_users WHERE email='new@example.test'")
    assert result["initial_password"] not in stored["password_hash"]

    refused = client.post("/v1/auth/users", json={"email": "third@example.test", "role": "lead"})
    assert refused.status_code == 409
    assert "Users limit of 2 reached" in refused.json()["detail"]
    assert "Raise the user limit in the Licence screen" in refused.json()["detail"]

    console = (Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "ui" / "console.html").read_text()
    assert 'id="users-create-form"' in console and "fetch('/v1/auth/users'" in console
    assert "S.users.error" in console and "S.users.initialPassword" in console
    assert has_permission("owner", "users.manage")


# --- AC1: endpoints --------------------------------------------------------------------------------------------------
def test_sign_in_sets_a_signed_strict_http_only_cookie_and_me_logout_work(ctx, seeded):
    client = make_client(ctx)
    assert "Sign in" in client.get("/login").text
    r = sign_in(client, seeded)
    assert r.status_code == 200 and r.json()["role"] == "super_admin" and r.json()["role_label"] == "Super Administrator"
    assert r.json()["email"] == EMAIL
    cookie = r.headers["set-cookie"].lower()
    assert COOKIE_NAME in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # public_url is http here
    me = client.get("/v1/auth/me")
    assert me.status_code == 200 and me.json()["email"] == EMAIL and me.json()["name"] == "Admin"
    assert user_row(ctx)["last_login_at"] is not None
    out = client.post("/v1/auth/logout")
    assert out.status_code == 200 and COOKIE_NAME in out.headers["set-cookie"] and "max-age=0" in out.headers["set-cookie"].lower()
    assert COOKIE_NAME not in client.cookies
    assert client.get("/v1/auth/me").status_code == 401


def test_secure_cookie_when_the_public_url_is_https(ctx, seeded):
    ctx.settings.public_url = "https://ai-cc.example.test"
    assert "secure" in sign_in(make_client(ctx), seeded).headers["set-cookie"].lower()


def test_missing_fields_are_refused(ctx, seeded):
    client = make_client(ctx)
    assert client.post("/v1/auth/login", json={"email": EMAIL}).status_code == 400
    assert client.post("/v1/auth/login", content=b"not json").status_code == 400


# --- AC2, AC3: same answer, lockout, lock expiry -----------------------------------------------------------------------
def test_unknown_email_wrong_password_locked_and_disabled_all_get_the_same_answer(ctx, seeded):
    client = make_client(ctx)
    unknown = sign_in(client, "whatever", "nobody@example.test")
    wrong = sign_in(client, "whatever")
    invalid = sign_in(client, "whatever", "not-an-email")
    ctx.db.run("UPDATE console_users SET failed_attempts=%s, locked_until=now() + interval '10 minutes'",
               (login.MAX_FAILURES,))
    locked = sign_in(client, seeded)  # locked, even with the right password
    ctx.db.run("UPDATE console_users SET locked_until=NULL, failed_attempts=0, disabled=true")
    disabled = sign_in(client, seeded)
    answers = [(r.status_code, r.json(), r.headers.get("retry-after")) for r in (unknown, wrong, invalid, locked, disabled)]
    assert answers == [(401, {"detail": "invalid email or password"}, None)] * 5
    assert "set-cookie" not in locked.headers and "set-cookie" not in disabled.headers


def test_repeated_failures_lock_the_account_and_a_reset_unlocks_it(ctx, seeded):
    client = make_client(ctx)
    for _ in range(login.MAX_FAILURES):
        assert sign_in(client, "whatever").status_code == 401
    row = user_row(ctx)
    assert row["failed_attempts"] == login.MAX_FAILURES and row["locked_until"] is not None
    assert sign_in(client, seeded).status_code == 401
    assert user_row(ctx)["failed_attempts"] == login.MAX_FAILURES  # a locked account counts no further failures
    again = login.reset_password(ctx.db, EMAIL)
    assert sign_in(client, again).status_code == 200
    assert user_row(ctx)["failed_attempts"] == 0


def test_an_expired_lock_resets_failed_attempts(ctx, seeded):
    client = make_client(ctx)
    ctx.db.run("UPDATE console_users SET failed_attempts=%s, locked_until=now() - interval '1 second'",
               (login.MAX_FAILURES,))
    assert sign_in(client, "whatever").status_code == 401
    row = user_row(ctx)
    assert row["failed_attempts"] == 1 and row["locked_until"] is None  # counting starts again, no instant re-lock
    assert sign_in(client, seeded).status_code == 200
    assert user_row(ctx)["failed_attempts"] == 0


# --- AC3: rate limits and bounded audit ------------------------------------------------------------------------------
def test_ip_and_email_rate_limit_answers_429_before_the_database(ctx, seeded, monkeypatch):
    ctx.settings.trusted_proxies = ["127.0.0.1/32"]
    monkeypatch.setattr(login, "RATE_PER_IP_EMAIL", 3)
    client = make_client(ctx)
    for _ in range(3):
        assert sign_in(client, "whatever", ip="203.0.113.7").status_code == 401
    lookups = []
    real_one = ctx.db.one
    monkeypatch.setattr(ctx.db, "one", lambda sql, *a: lookups.append(sql) or real_one(sql, *a))
    limited = sign_in(client, seeded, ip="203.0.113.7")
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    assert not [s for s in lookups if "console_users" in s]  # refused before any lookup
    assert user_row(ctx)["failed_attempts"] == 3
    assert sign_in(client, seeded, ip="198.51.100.9").status_code == 200  # another address is not affected


def test_ip_rate_limit_covers_every_email(ctx, monkeypatch):
    ctx.settings.trusted_proxies = ["127.0.0.1/32"]
    monkeypatch.setattr(login, "RATE_PER_IP", 4)
    client = make_client(ctx)
    codes = [sign_in(client, "x", f"u{i}@example.test", ip="203.0.113.8").status_code for i in range(6)]
    assert codes == [401, 401, 401, 401, 429, 429]
    assert sign_in(client, "x", "u9@example.test", ip="203.0.113.9").status_code == 401


def test_failed_sign_in_audit_writes_are_bounded(ctx, monkeypatch):
    monkeypatch.setattr(login, "AUDIT_FAILED_PER_MIN", 3)
    client = make_client(ctx)
    for i in range(12):
        assert sign_in(client, "x", f"u{i}@example.test", ip=f"203.0.113.{i}").status_code == 401
    rows = ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE target='console' AND NOT accepted")["n"]
    assert 3 <= rows <= 6  # at most the limit per minute (the run may straddle one minute boundary)


def test_login_audit_never_holds_the_password(ctx, seeded):
    client = make_client(ctx)
    sign_in(client, "bad-password-123")
    sign_in(client, seeded)
    rows = ctx.db.all("SELECT actor, accepted, result, payload::text AS payload FROM write_audit"
                      " WHERE target='console' ORDER BY id")
    assert [r["accepted"] for r in rows] == [False, True]
    assert all("bad-password-123" not in (r["result"] + r["payload"]) and seeded not in r["result"] + r["payload"] for r in rows)


# --- AC4: scrypt off the event loop ----------------------------------------------------------------------------------
def test_scrypt_runs_off_the_event_loop_for_known_and_unknown_emails(ctx, seeded, monkeypatch):
    on_loop = []
    real = login.verify_password

    def verify(password, stored):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(password, stored)

    monkeypatch.setattr(login, "verify_password", verify)
    client = make_client(ctx)
    assert sign_in(client, seeded).status_code == 200
    assert sign_in(client, "x", "nobody@example.test").status_code == 401  # the dummy hash
    assert on_loop == [False, False]


# --- AC5: roles --------------------------------------------------------------------------------------------------------
def test_super_administrator_passes_every_role_check_and_lead_does_not():
    assert role_satisfies("super_admin", {"integrations.manage"}) and role_satisfies("super_admin", {"console.read"})
    assert role_satisfies("owner", {"integrations.manage", "console.read"})
    assert not role_satisfies("lead", {"integrations.manage"})
    assert not role_satisfies("unknown", {"integrations.manage"})
    assert has_permission("super_admin", "anything.at.all") and has_permission("owner", "nodes.manage")
    assert not has_permission("lead", "nodes.manage")


def test_non_owner_role_gets_endpoint_access_from_its_mapped_permissions(ctx, monkeypatch):
    monkeypatch.setitem(console_auth.ROLE_PERMISSIONS, "integration_admin", frozenset({"integrations.manage"}))
    monkeypatch.setattr(console_auth, "ALLOWED_ROLES", console_auth.ALLOWED_ROLES | {"integration_admin"})
    client = make_client(ctx)
    client.cookies.set(COOKIE_NAME, issue_session("a@example.test", "integration_admin", EXP, SECRET))
    assert client.get("/v1/integrations/github").status_code == 200
    assert client.get("/v1/projects/catalog").status_code == 403
    session = client.get("/v1/console/session")
    assert session.status_code == 200 and session.json()["permissions"] == ["integrations.manage"]


def test_console_ui_uses_session_permission_identifiers_for_controls():
    html = (Path(login.__file__).parent / "ui" / "console.html").read_text(encoding="utf-8")
    assert "function consoleCan(permission)" in html
    assert "S.consoleUser && S.consoleUser.permissions" in html
    assert "permissions.indexOf('*') >= 0 || permissions.indexOf(permission) >= 0" in html
    assert all(f"consoleCan('{permission}')" in html for permission in (
        "console.read", "integrations.manage", "nodes.manage", "projects.manage"))
    assert "S.consoleUser.role === 'owner'" not in html
    assert "S.consoleUser.role === 'lead'" not in html


def test_owner_only_endpoints_accept_super_admin(ctx):
    client = make_client(ctx)

    def get(role, path="/v1/integrations/github"):
        client.cookies.set(COOKIE_NAME, issue_session("a@example.test", role, EXP, SECRET))
        return client.get(path).status_code

    assert get("lead") == 403
    assert get("owner") == 200 and get("super_admin") == 200
    assert get("lead", "/v1/integrations/jira") == 403 and get("super_admin", "/v1/integrations/jira") != 403
    client.cookies.set(COOKIE_NAME, issue_session("a@example.test", "super_admin", EXP, SECRET))
    session = client.get("/v1/console/session").json()
    assert session["role"] == "super_admin" and session["permissions"] == ["*"]


# --- AC6: the gate -----------------------------------------------------------------------------------------------------
def test_without_the_setting_nothing_is_gated(ctx):
    client = make_client(ctx, gate=False)
    assert client.get("/v1/state").status_code == 200
    assert client.get("/cc").status_code not in (302, 401)


def test_state_accepts_only_the_owner_token_when_sign_in_is_on(ctx):
    signed_in_gate = make_client(ctx, gate=True)
    assert signed_in_gate.get("/v1/state").status_code == 401
    assert signed_in_gate.get("/v1/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert signed_in_gate.get("/v1/state", headers={"Authorization": "Bearer owner-secret"}).status_code == 200

    sign_in_off = make_client(ctx, gate=False)
    assert sign_in_off.get("/v1/state").status_code == 200


def test_gate_redirects_pages_and_rejects_api_calls_without_a_session(ctx, seeded):
    client = make_client(ctx, gate=True)
    r = client.get("/cc")
    assert r.status_code == 302 and r.headers["location"] == "/login?next=%2Fcc"
    assert client.get("/delivery?x=1").headers["location"] == "/login?next=%2Fdelivery%3Fx%3D1"
    assert client.get("/machines").headers["location"] == "/login?next=%2Fmachines"
    assert client.get("/v1/state").status_code == 401
    assert client.get("/v1/state", headers={"Authorization": "Bearer not-a-token-route"}).status_code == 401
    client.cookies.set(COOKIE_NAME, "forged.cookie.value")
    assert client.get("/v1/state").status_code == 401
    client.cookies.clear()
    for open_path in ("/healthz", "/login"):
        assert client.get(open_path).status_code == 200
    assert client.get("/v1/auth/me").status_code == 401
    assert sign_in(client, seeded).status_code == 200
    assert client.get("/v1/state").status_code == 200 and client.get("/cc").status_code not in (302, 401)


def test_gate_lets_node_tokens_through_on_token_routes(ctx):
    client = make_client(ctx, gate=True)
    assert client.get("/v1/work/candidates", params={"account": "claude-01"}).status_code == 401
    joined = join_node(client, "node-gate")
    assert joined.status_code == 200      # join grants carry their own one-use token
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    assert client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers).status_code != 401


def test_policy_decide_with_a_bearer_header_passes_the_gate(ctx):
    client = make_client(ctx, gate=True)
    body = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert client.post("/v1/policy/decide", json=body).json() == {"detail": "sign in required"}
    r = client.post("/v1/policy/decide", json=body, headers={"Authorization": "Bearer unknown-run-token"})
    assert r.status_code == 200  # the handler answered, and it checks the token itself
    assert r.json()["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_owner_token_jira_routes_pass_the_gate_and_still_check_the_token(ctx):
    client = make_client(ctx, gate=True)
    assert client.get("/v1/integrations/jira").json() == {"detail": "sign in required"}
    wrong = client.get("/v1/integrations/jira", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401 and wrong.json()["detail"] == "owner authorization required"
    owner = client.get("/v1/integrations/jira", headers={"Authorization": "Bearer owner-secret"})
    assert owner.status_code != 401
    # consent callbacks come back from Atlassian or GitHub without the Strict cookie; a one-use state protects them
    for path in ("/v1/integrations/jira/oauth/callback", "/v1/integrations/github/manifest/callback?code=x&state=y",
                 "/v1/integrations/github/setup?installation_id=1&state=y"):
        r = client.get(path)
        assert r.status_code != 401 and "sign in required" not in r.text, path


def test_next_is_only_a_path_on_this_site():
    assert login.safe_next("/delivery?x=1") == "/delivery?x=1"
    for bad in ("https://evil.example", "//evil.example", "/\\evil", "javascript:alert(1)", ""):
        assert login.safe_next(bad) == "/cc"


def test_forwarded_client_ip_headers_are_ignored_unless_the_peer_is_a_trusted_proxy():
    def request(peer, headers):
        return Request({"type": "http", "method": "POST", "path": "/v1/auth/login", "headers": headers,
                        "client": (peer, 1234), "server": ("origin", 80), "scheme": "http"})

    direct = request("198.51.100.20", [(b"cf-connecting-ip", b"203.0.113.9"),
                                       (b"x-forwarded-for", b"192.0.2.6")])
    assert login.client_ip(direct, ["10.0.0.0/8"]) == "198.51.100.20"

    proxied = request("10.0.0.5", [(b"cf-connecting-ip", b"203.0.113.9")])
    assert login.client_ip(proxied, ["10.0.0.0/8"]) == "203.0.113.9"

    fallback = request("10.0.0.5", [(b"cf-connecting-ip", b"not-an-ip"),
                                    (b"x-forwarded-for", b"192.0.2.6, 10.0.0.4")])
    assert login.client_ip(fallback, ["10.0.0.0/8"]) == "192.0.2.6"


def test_trusted_proxy_cidrs_load_from_settings(monkeypatch):
    monkeypatch.setenv("AIFLOW4_TRUSTED_PROXIES", "10.12.0.0/16, 2001:db8::5/128")
    assert Settings.from_env().trusted_proxies == ["10.12.0.0/16", "2001:db8::5/128"]


def test_untrusted_forwarding_headers_cannot_rotate_the_ip_rate_limit(ctx, monkeypatch):
    monkeypatch.setattr(login, "RATE_PER_IP", 2)
    client = make_client(ctx)
    codes = [sign_in(client, "x", f"u{i}@example.test", ip=f"203.0.113.{i}").status_code for i in range(4)]
    assert codes == [401, 401, 429, 429]


# --- AC7: the seed command ---------------------------------------------------------------------------------------------
def seed_args(path, action="seed"):
    return SimpleNamespace(action=action, email=EMAIL, role="super_admin", name="Admin", password_file=str(path))


def test_cli_seed_writes_the_password_to_a_private_file_prints_nothing_secret_and_is_idempotent(
        ctx, tmp_path, capsys, test_database_url):
    settings = SimpleNamespace(database_url=test_database_url)
    pw = tmp_path / "pw"
    assert cli._console_user(seed_args(pw), settings) == 0
    secret = pw.read_text().strip()
    assert len(secret) >= 32 and stat.S_IMODE(os.stat(pw).st_mode) == 0o600
    out = capsys.readouterr()
    assert secret not in out.out + out.err and "mode 0600" in out.out
    before = ctx.db.one("SELECT password_hash, updated_at FROM console_users")
    assert cli._console_user(seed_args(pw), settings) == 0
    assert "already exists" in capsys.readouterr().out
    assert pw.read_text().strip() == secret and ctx.db.one("SELECT password_hash, updated_at FROM console_users") == before
    assert login.verify_password(secret, before["password_hash"])


def test_cli_seed_makes_an_existing_password_file_private(ctx, tmp_path, test_database_url):
    pw = tmp_path / "pw"
    pw.write_text("old contents that are much longer than a password\n")
    os.chmod(pw, 0o644)
    assert cli._console_user(seed_args(pw), SimpleNamespace(database_url=test_database_url)) == 0
    assert stat.S_IMODE(os.stat(pw).st_mode) == 0o600
    assert login.verify_password(pw.read_text().strip(), ctx.db.one("SELECT password_hash FROM console_users")["password_hash"])


def test_cli_seed_refuses_a_symlink_and_creates_no_user(ctx, tmp_path, capsys, test_database_url):
    target = tmp_path / "elsewhere"
    target.write_text("keep me\n")
    link = tmp_path / "pw"
    link.symlink_to(target)
    assert cli._console_user(seed_args(link), SimpleNamespace(database_url=test_database_url)) == 2
    assert target.read_text() == "keep me\n" and link.is_symlink()
    assert ctx.db.one("SELECT count(*) AS n FROM console_users")["n"] == 0
    assert "nothing changed" in capsys.readouterr().err


def test_password_file_is_written_before_the_commit(ctx, tmp_path, monkeypatch, test_database_url):
    settings = SimpleNamespace(database_url=test_database_url)
    seen = []

    def failing(path, password):
        # the new user is not visible to other connections yet: the write happens before the commit
        seen.append(ctx.db.one("SELECT count(*) AS n FROM console_users")["n"])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(login, "write_password_file", failing)
    assert cli._console_user(seed_args(tmp_path / "pw"), settings) == 2
    assert seen == [0] and ctx.db.one("SELECT count(*) AS n FROM console_users")["n"] == 0
    # reset-password: a failed write keeps the old, working password
    old = login.seed_user(ctx.db, EMAIL, "super_admin")
    assert cli._console_user(seed_args(tmp_path / "pw", "reset-password"), settings) == 2
    assert login.verify_password(old, ctx.db.one("SELECT password_hash FROM console_users")["password_hash"])
    monkeypatch.undo()
    assert cli._console_user(seed_args(tmp_path / "pw", "reset-password"), settings) == 0
    new = (tmp_path / "pw").read_text().strip()
    assert new != old and login.verify_password(new, ctx.db.one("SELECT password_hash FROM console_users")["password_hash"])


def test_write_password_file_rejects_files_that_are_not_private_regular_files(tmp_path):
    (tmp_path / "a").write_text("x")
    os.link(tmp_path / "a", tmp_path / "hardlink")
    with pytest.raises(ValueError):
        login.write_password_file(str(tmp_path / "hardlink"), "secret-value")
    assert (tmp_path / "a").read_text() == "x"
    with pytest.raises(ValueError):
        login.write_password_file(str(tmp_path / "missing-dir" / "pw"), "secret-value")
