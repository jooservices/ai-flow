"""Security hardening (JAIF-231): two-factor sign-in, sessions, encrypted secrets, rate limits, CSRF, audit log."""
import json
import re
import time

import pytest
import redis
from fastapi.testclient import TestClient

from aiflow.v4.control import audit, login, mfa
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import COOKIE_NAME, csrf_token, issue_session
from aiflow.v4.control.github import GitHubApp
from aiflow.v4.control.github_connect import GitHubSecretStore
from aiflow.v4.control.jira_connect import SecretStore
from aiflow.v4.core import secretbox

SECRET = "s" * 40
ADMIN = "admin@example.test"
OTHER = "lead@example.test"


def make_client(ctx, gate=False, ip="127.0.0.1", require_mfa=False):
    ctx.settings.console_session_secret = SECRET
    ctx.settings.console_login = gate
    ctx.settings.console_require_mfa = require_mfa  # exercise legacy settings, which must not weaken administrator MFA
    app = create_app(ctx)
    app.state.sync_accounts()
    return TestClient(app, follow_redirects=False, client=(ip, 50000))


def sign_in(client, password, email=ADMIN, code=""):
    return client.post("/v1/auth/login", json={"email": email, "password": password, "code": code})


@pytest.fixture
def key(monkeypatch):
    value = secretbox.generate_key()
    monkeypatch.setenv("AIFLOW4_SECRET_KEY", value)
    return value


@pytest.fixture
def admin(ctx):
    password = login.seed_user(ctx.db, ADMIN, "super_admin", "Admin")
    return password


def enrol(client, admin_password, email=ADMIN, step_offset=-30):
    """Turn on two-factor for ADMIN on a signed-in client; returns (totp secret, recovery codes)."""
    assert sign_in(client, admin_password, email).status_code == 200
    secret = client.post("/v1/security/mfa/setup").json()["secret"]
    # the enrolment code is from the previous time step so the current and next steps are still unused
    r = client.post("/v1/security/mfa/enable", json={"code": mfa.totp_code(secret, time.time() + step_offset)})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"]


# --- AC1: two-factor sign-in and recovery codes ---------------------------------------------------------------------
def test_totp_matches_the_rfc_6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # ASCII "12345678901234567890"
    assert mfa.totp_code(secret, 59) == "287082"  # RFC 6238 appendix B: 94287082, last six digits
    assert mfa.match_step(secret, mfa.totp_code(secret, 1_111_111_109), now=1_111_111_109) == 1_111_111_109 // 30
    assert mfa.match_step(secret, "000000", now=1_111_111_109) is None


def test_two_factor_setup_enable_and_sign_in_needs_the_code(ctx, key, admin):
    owner = make_client(ctx)
    secret, codes = enrol(owner, admin)
    assert len(codes) == 10 and all(re.fullmatch(r"[a-z2-9]{4}(-[a-z2-9]{4}){2}", c) for c in codes)
    assert owner.get("/v1/security/mfa").json()["enabled"] is True
    setup_audit = ctx.db.one("SELECT actor, category, target, detail FROM audit_log WHERE action='mfa_setup'")
    assert setup_audit == {"actor": ADMIN, "category": "security", "target": ADMIN, "detail": {}}
    assert secret not in json.dumps(ctx.db.all("SELECT * FROM audit_log"), default=str)

    fresh = make_client(ctx)
    needs = sign_in(fresh, admin)
    assert needs.status_code == 401 and needs.json()["mfa_required"] is True and "set-cookie" not in needs.headers
    assert sign_in(fresh, admin, code="000000").status_code == 401
    ok = sign_in(fresh, admin, code=mfa.totp_code(secret))
    assert ok.status_code == 200 and COOKIE_NAME in ok.headers["set-cookie"]
    # the same code is not accepted twice
    again = make_client(ctx)
    assert sign_in(again, admin, code=mfa.totp_code(secret)).status_code == 401
    # a wrong password with a right code gets the generic refusal
    other = make_client(ctx)
    refused = sign_in(other, "wrong", code=mfa.totp_code(secret, time.time() + 30))
    assert refused.status_code == 401 and refused.json() == {"detail": login.REFUSED}


def test_pending_mfa_secret_rolls_back_when_its_audit_entry_cannot_be_written(ctx, admin, monkeypatch):
    signed_in = make_client(ctx)
    assert sign_in(signed_in, admin).status_code == 200
    client = TestClient(signed_in.app, follow_redirects=False, raise_server_exceptions=False)
    client.cookies.update(signed_in.cookies)

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(audit, "record", fail_append)
    assert client.post("/v1/security/mfa/setup").status_code == 500
    row = ctx.db.one("SELECT totp_secret FROM console_users WHERE email=%s", (ADMIN,))
    assert row["totp_secret"] == ""
    assert ctx.db.one("SELECT 1 FROM audit_log WHERE action='mfa_setup'") is None


def test_administrator_mfa_is_required_by_default(ctx, admin):
    assert ctx.settings.console_require_mfa is True
    assert login.require_mfa(ctx) is True
    client = make_client(ctx, require_mfa=True)
    started = sign_in(client, admin)
    assert started.status_code == 200 and started.json()["mfa_setup_required"] is True
    assert client.get("/v1/console/session").status_code == 401
    assert client.post("/v1/security/mfa/setup").status_code == 200


def test_recovery_codes_sign_in_once_and_can_be_reissued(ctx, key, admin):
    secret, codes = enrol(make_client(ctx), admin)
    a = make_client(ctx)
    ok = sign_in(a, admin, code=codes[0].upper())  # case and dashes do not matter
    assert ok.status_code == 200 and ok.json()["recovery_codes_left"] == 9
    assert sign_in(make_client(ctx), admin, code=codes[0]).status_code == 401  # used
    assert sign_in(make_client(ctx), admin, code="aaaa-aaaa-aaaa").status_code == 401  # unknown
    new = a.post("/v1/security/mfa/recovery-codes", json={"code": codes[1]})
    assert new.status_code == 200 and len(new.json()["recovery_codes"]) == 10
    assert sign_in(make_client(ctx), admin, code=codes[2]).status_code == 401  # the old set is gone
    assert a.post("/v1/security/mfa/recovery-codes", json={"code": "nope"}).status_code == 403


def test_turning_two_factor_off_needs_a_code_and_an_admin_can_reset_it(ctx, key, admin):
    secret, codes = enrol(make_client(ctx), admin)
    other_password = login.seed_user(ctx.db, OTHER, "lead")
    lead = make_client(ctx)
    assert sign_in(lead, other_password, OTHER).status_code == 200
    lead_secret = lead.post("/v1/security/mfa/setup").json()["secret"]
    lead.post("/v1/security/mfa/enable", json={"code": mfa.totp_code(lead_secret)})
    assert lead.post("/v1/security/mfa/disable", json={"code": "000000"}).status_code == 403
    # the lead lost the phone and the codes: an authorised user resets it
    boss = make_client(ctx)
    assert sign_in(boss, admin, code=codes[0]).status_code == 200
    assert lead.post("/v1/security/users/" + OTHER + "/mfa/reset").status_code == 403  # a lead cannot
    assert boss.post("/v1/security/users/" + OTHER + "/mfa/reset").json() == {"reset": True}
    assert lead.get("/v1/auth/me").status_code == 401  # signed out by the reset
    assert sign_in(make_client(ctx), other_password, OTHER).status_code == 200  # back to password only
    assert ctx.db.one("SELECT count(*) AS n FROM console_recovery_codes WHERE email=%s", (OTHER,))["n"] == 0


def test_administrator_mfa_cannot_be_disabled_by_the_legacy_setting(ctx, key, admin):
    login.set_setting(ctx.db, "require_mfa", "0", ADMIN)  # a prior release or direct database edit
    client = make_client(ctx, gate=True, require_mfa=False)
    started = sign_in(client, admin)
    assert started.status_code == 200 and started.json()["mfa_setup_required"] is True
    assert client.get("/v1/state").status_code == 403  # the gate permits only two-factor setup
    assert client.get("/v1/console/session").status_code == 403
    secret = client.post("/v1/security/mfa/setup").json()["secret"]
    enabled = client.post("/v1/security/mfa/enable", json={"code": mfa.totp_code(secret)})
    assert enabled.status_code == 200 and client.get("/v1/console/session").status_code == 200
    assert client.get("/v1/security/settings").json()["require_mfa"] is True
    assert client.put("/v1/security/settings", json={"require_mfa": False}).status_code == 422
    assert ctx.db.one("SELECT value FROM security_settings WHERE key='require_mfa'")["value"] == "0"
    assert client.post("/v1/security/mfa/disable", json={"code": mfa.totp_code(secret)}).status_code == 403
    fresh = make_client(ctx, gate=True, require_mfa=False)
    needs = sign_in(fresh, admin)
    assert needs.status_code == 401 and needs.json()["mfa_required"] is True


def test_session_length_is_a_setting_and_applies_to_the_next_sign_in(ctx, admin):
    client = make_client(ctx)
    secret, _ = enrol(client, admin)
    assert client.put("/v1/security/settings", json={"session_ttl_s": 10}).status_code == 422  # below the minimum
    assert client.put("/v1/security/settings", json={"session_ttl_s": 3600}).json()["session_ttl_s"] == 3600
    r = sign_in(make_client(ctx), admin, code=mfa.totp_code(secret))
    assert "max-age=3600" in r.headers["set-cookie"].lower()
    row = ctx.db.one("SELECT extract(epoch FROM expires_at - created_at) AS s FROM console_sessions ORDER BY created_at DESC")
    assert 3590 <= float(row["s"]) <= 3610


def test_forced_sign_out_ends_the_session_and_the_next_request_is_rejected(ctx, admin):
    target, boss = make_client(ctx), make_client(ctx)  # the login gate is off: the check must not rely on it
    secret, _ = enrol(target, admin)
    assert sign_in(boss, admin, code=mfa.totp_code(secret)).status_code == 200
    assert target.get("/v1/console/session").status_code == 200
    sessions = boss.get("/v1/security/sessions").json()["sessions"]
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    victim = next(s for s in sessions if not s["current"])
    assert boss.post(f"/v1/security/sessions/{victim['sid']}/revoke").json() == {"revoked": 1}
    assert target.get("/v1/console/session").status_code == 401
    assert target.get("/v1/auth/me").status_code == 401
    assert target.get("/v1/security/mfa").status_code == 401
    assert boss.get("/v1/console/session").status_code == 200  # the other session is untouched
    assert ctx.db.one("SELECT action FROM audit_log WHERE action='forced_sign_out'")


def test_forced_sign_out_through_the_gate_and_sign_out_everywhere(ctx, admin):
    target, boss = make_client(ctx, gate=True), make_client(ctx, gate=True)
    secret, _ = enrol(target, admin)
    assert sign_in(boss, admin, code=mfa.totp_code(secret)).status_code == 200
    assert target.get("/v1/state").status_code == 200
    assert boss.post(f"/v1/security/users/{ADMIN}/sign-out").json()["revoked"] == 2
    assert target.get("/v1/state").status_code == 401 and boss.get("/v1/state").status_code == 401
    assert target.get("/dashboard").status_code == 302


def test_logout_and_password_reset_end_the_session(ctx, admin):
    a = make_client(ctx)
    secret, _ = enrol(a, admin)
    stolen = a.cookies.get(COOKIE_NAME)
    assert a.get("/logout").status_code == 405
    assert a.get("/v1/auth/me").status_code == 200  # a safe request cannot end the session
    assert a.post("/logout").status_code == 303
    thief = make_client(ctx)
    thief.cookies.set(COOKIE_NAME, stolen)
    assert thief.get("/v1/console/session").status_code == 401  # a copied cookie dies with the logout
    b = make_client(ctx)
    assert sign_in(b, admin, code=mfa.totp_code(secret)).status_code == 200
    assert b.post("/v1/auth/logout").status_code == 200
    assert sign_in(b, admin, code=mfa.totp_code(secret, time.time() + 30)).status_code == 200
    login.reset_password(ctx.db, ADMIN)
    assert b.get("/v1/console/session").status_code == 401


def test_only_authorised_people_manage_security(ctx):
    lead_pw = login.seed_user(ctx.db, OTHER, "lead")
    owner_pw = login.seed_user(ctx.db, "owner@example.test", "owner")
    for email, password in ((OTHER, lead_pw), ("owner@example.test", owner_pw)):
        c = make_client(ctx)
        if email == "owner@example.test":
            enrol(c, password, email=email)
        else:
            assert sign_in(c, password, email).status_code == 200
        assert c.get("/v1/security/settings").status_code == 403
        assert c.put("/v1/security/settings", json={"session_ttl_s": 600}).status_code == 403
        assert c.get("/v1/security/sessions").status_code == 403
        assert c.get("/v1/security/audit").status_code == 403
        assert c.get("/v1/security/mfa").status_code == 200  # everyone can see and set their own two-factor
    assert make_client(ctx).get("/v1/security/settings").status_code == 401


# --- AC2: secrets encrypted at rest ---------------------------------------------------------------------------------
def test_secretbox_round_trip_wrong_key_and_legacy_plaintext(key):
    sealed = secretbox.encrypt("hunter2-token", context="a")
    assert "hunter2" not in sealed and secretbox.is_encrypted(sealed) and sealed != secretbox.encrypt("hunter2-token", context="a")
    assert secretbox.decrypt(sealed, context="a") == "hunter2-token"
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt(sealed, context="other")  # bound to its place
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt(sealed, secretbox._decode_key(secretbox.generate_key()), "a")
    assert secretbox.decrypt("plain legacy value") == "plain legacy value"
    with pytest.raises(secretbox.SecretBoxError):
        secretbox._decode_key("short")


def test_key_can_come_from_a_file_and_is_never_taken_from_the_database(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFLOW4_SECRET_KEY", raising=False)
    monkeypatch.delenv("AIFLOW4_SECRET_KEY_FILE", raising=False)
    assert secretbox.load_key() is None
    with pytest.raises(secretbox.SecretBoxError, match="no secret encryption key"):
        secretbox.seal("x")
    keyfile = tmp_path / "secret.key"
    keyfile.write_text(secretbox.generate_key() + "\n")
    monkeypatch.setenv("AIFLOW4_SECRET_KEY_FILE", str(keyfile))
    assert secretbox.is_encrypted(secretbox.seal("x"))
    monkeypatch.setenv("AIFLOW4_SECRET_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.load_key()


def test_connector_secret_writes_require_a_configured_external_key(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFLOW4_SECRET_KEY", raising=False)
    monkeypatch.delenv("AIFLOW4_SECRET_KEY_FILE", raising=False)
    with pytest.raises(secretbox.SecretBoxError, match="no secret encryption key"):
        SecretStore(tmp_path / "jira").save_credential({"access_token": "secret"})
    with pytest.raises(secretbox.SecretBoxError, match="no secret encryption key"):
        GitHubSecretStore(tmp_path / "github").save_active({"id": 1}, "private key")
    assert not (tmp_path / "jira" / "credential.json").exists()
    assert not (tmp_path / "github" / "app.pem").exists()


def test_jira_credentials_are_stored_encrypted(tmp_path, key, monkeypatch):
    store = SecretStore(tmp_path / "jira")
    store.save_credential({"kind": "oauth", "access_token": "at-SECRET-1", "refresh_token": "rt-SECRET-2"})
    store.save_app("client-id", "client-SECRET-3")
    for name in ("credential.json", "oauth-app.json"):
        raw = (tmp_path / "jira" / name).read_text()
        assert secretbox.is_encrypted(raw) and "SECRET" not in raw
    assert store.credential()["refresh_token"] == "rt-SECRET-2" and store.app()["client_secret"] == "client-SECRET-3"
    monkeypatch.setenv("AIFLOW4_SECRET_KEY", secretbox.generate_key())  # a copy of the files without the key
    assert store.credential() is None


def test_existing_jira_plaintext_is_migrated_when_read(tmp_path, key):
    store = SecretStore(tmp_path / "jira")
    store.dir.mkdir()
    legacy = json.dumps({"access_token": "legacy-SECRET", "refresh_token": "legacy-REFRESH"})
    (store.dir / store.CREDENTIAL).write_text(legacy, encoding="utf-8")
    assert store.credential() == {"access_token": "legacy-SECRET", "refresh_token": "legacy-REFRESH"}
    raw = (store.dir / store.CREDENTIAL).read_text(encoding="utf-8")
    assert secretbox.is_encrypted(raw) and "legacy-SECRET" not in raw


def test_github_app_key_is_stored_encrypted(tmp_path, key):
    store = GitHubSecretStore(tmp_path / "gh")
    pem = "-----BEGIN PRIVATE KEY-----\nPEM-SECRET\n-----END PRIVATE KEY-----\n"
    store.save_active({"id": 1, "slug": "aiflow", "installation_id": 2}, pem)
    assert "PEM-SECRET" not in (tmp_path / "gh" / "app.pem").read_text()
    store.create_pending("a" * 64, {"app": {"id": 1}}, pem)
    assert store.read_pending("a" * 64)[2] == pem


def test_existing_github_private_key_is_migrated_when_loaded(tmp_path, key):
    root = tmp_path / "gh"
    root.mkdir()
    pem = "-----BEGIN PRIVATE KEY-----\nLEGACY-PEM-SECRET\n-----END PRIVATE KEY-----\n"
    (root / "app.json").write_text(json.dumps({"id": "1", "installation_id": 2}), encoding="utf-8")
    (root / "app.pem").write_text(pem, encoding="utf-8")
    app = GitHubApp(str(root), "aiflow")
    raw = (root / "app.pem").read_text(encoding="utf-8")
    assert app.key == pem and secretbox.is_encrypted(raw) and "LEGACY-PEM-SECRET" not in raw


def test_two_factor_secrets_and_recovery_codes_are_not_readable_in_the_database(ctx, key, admin):
    secret, codes = enrol(make_client(ctx), admin)
    dump = json.dumps(ctx.db.all("SELECT * FROM console_users") + ctx.db.all("SELECT * FROM console_recovery_codes"),
                      default=str)
    assert secret not in dump and all(c not in dump and c.replace("-", "") not in dump for c in codes)
    assert secretbox.is_encrypted(ctx.db.one("SELECT totp_secret FROM console_users")["totp_secret"])
    # without the environment key the value cannot be read, even by the application
    row = ctx.db.one("SELECT * FROM console_users")
    assert mfa.read_secret(row, ADMIN, SECRET) == secret


def test_existing_plaintext_totp_secret_is_migrated_at_control_startup(ctx, key, admin):
    legacy = mfa.new_secret()
    ctx.db.run("UPDATE console_users SET totp_secret=%s, totp_enabled=true WHERE email=%s", (legacy, ADMIN))
    make_client(ctx, require_mfa=True)
    row = ctx.db.one("SELECT email, totp_secret FROM console_users WHERE email=%s", (ADMIN,))
    assert secretbox.is_encrypted(row["totp_secret"])
    assert mfa.read_secret(row, ADMIN, SECRET) == legacy


# --- AC3: rate limits and sign-in guessing protection ----------------------------------------------------------------
def test_public_endpoints_are_rate_limited(ctx):
    ctx.settings.rate_limit_public_per_min = 3
    client = make_client(ctx)
    assert [client.get("/healthz").status_code for _ in range(3)] == [200, 200, 200]
    over = client.get("/healthz")
    assert over.status_code == 429 and int(over.headers["retry-after"]) >= 1
    assert client.get("/v1/state").status_code == 200  # a path that needs no public budget is unaffected


def test_every_endpoint_has_a_per_address_budget(ctx):
    ctx.settings.rate_limit_per_min = 5
    client = make_client(ctx)
    codes = [client.get("/v1/state").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    ctx.settings.rate_limit_per_min = 0  # a limit of 0 turns the budget off
    assert make_client(ctx, ip="10.0.0.9").get("/v1/state").status_code == 200


def test_rate_limiter_fails_closed_when_redis_is_unavailable(ctx, monkeypatch):
    ctx.settings.rate_limit_public_per_min = 3
    client = make_client(ctx)

    def redis_down(*_args, **_kwargs):
        raise redis.ConnectionError("test outage")

    monkeypatch.setattr(ctx.leases.r, "pipeline", redis_down)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


def test_anonymous_changes_have_a_tighter_budget(ctx):
    ctx.settings.rate_limit_public_per_min = 2
    client = make_client(ctx)
    statuses = [client.post("/v1/nodes/join-tokens/inspect", json={"join_token": "x"}).status_code for _ in range(3)]
    assert statuses[-1] == 429


def test_sign_in_attempts_per_address_and_email_are_capped(ctx, admin):
    client = make_client(ctx)
    statuses = [sign_in(client, "wrong").status_code for _ in range(login.RATE_PER_IP_EMAIL + 1)]
    assert statuses[-1] == 429
    assert "retry-after" in sign_in(client, admin).headers


def test_valid_second_factor_can_sign_in_after_sign_in_budgets_are_exhausted(ctx, key, admin, monkeypatch):
    monkeypatch.setattr(login, "RATE_PER_IP", 2)
    monkeypatch.setattr(login, "RATE_PER_IP_EMAIL", 2)
    secret, codes = enrol(make_client(ctx, ip="127.0.0.9"), admin)
    client = make_client(ctx, ip="127.0.0.10")
    assert sign_in(client, "wrong").status_code == 401
    assert sign_in(client, "wrong").status_code == 401
    limited = sign_in(client, "wrong")
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    valid_totp = sign_in(client, admin, code=mfa.totp_code(secret))
    assert valid_totp.status_code == 200

    recovery_client = make_client(ctx, ip="127.0.0.11")
    for _ in range(3):
        assert sign_in(recovery_client, "wrong").status_code in (401, 429)
    valid_recovery = sign_in(recovery_client, admin, code=codes[0])
    assert valid_recovery.status_code == 200


def test_guessing_locks_password_only_sign_in_but_never_the_real_owner(ctx, key, admin):
    secret, codes = enrol(make_client(ctx), admin)
    attacker = make_client(ctx)
    for _ in range(login.MAX_FAILURES):
        assert sign_in(attacker, "guess").status_code == 401
    row = ctx.db.one("SELECT locked_until FROM console_users WHERE email=%s", (ADMIN,))
    assert row["locked_until"] is not None
    # the right password alone is refused while locked, with the same answer as a wrong one, and reveals nothing
    locked = sign_in(make_client(ctx), admin)
    assert locked.status_code == 401 and locked.json() == {"detail": login.REFUSED}
    # guessing with a made-up code does not get in either, and does not extend the lock
    assert sign_in(make_client(ctx), admin, code="123456").status_code == 401
    # the owner has the second factor: authenticator code ...
    owner = make_client(ctx)
    ok = sign_in(owner, admin, code=mfa.totp_code(secret))
    assert ok.status_code == 200 and owner.get("/v1/console/session").status_code == 200
    assert ctx.db.one("SELECT locked_until, failed_attempts FROM console_users WHERE email=%s", (ADMIN,)) == {
        "locked_until": None, "failed_attempts": 0}  # success clears the lock


def test_the_owner_can_use_a_recovery_code_while_the_account_is_locked(ctx, key, admin):
    secret, codes = enrol(make_client(ctx), admin)
    for _ in range(login.MAX_FAILURES):
        sign_in(make_client(ctx), "guess")
    assert ctx.db.one("SELECT locked_until FROM console_users")["locked_until"] is not None
    ok = sign_in(make_client(ctx, ip="127.0.0.2"), admin, code=codes[3])
    assert ok.status_code == 200


def test_an_account_without_two_factor_stays_locked_until_the_lock_expires_or_an_admin_unlocks(ctx, admin):
    for _ in range(login.MAX_FAILURES):
        sign_in(make_client(ctx), "guess")
    assert sign_in(make_client(ctx), admin, code="123456").status_code == 401  # a code means nothing without two-factor
    helper = login.seed_user(ctx.db, OTHER, "super_admin")
    boss = make_client(ctx, ip="127.0.0.3")
    helper_secret, _ = enrol(boss, helper, email=OTHER)
    assert sign_in(boss, helper, OTHER, code=mfa.totp_code(helper_secret)).status_code == 200
    assert boss.post(f"/v1/security/users/{ADMIN}/unlock").json() == {"unlocked": True}
    assert sign_in(make_client(ctx, ip="127.0.0.4"), admin).status_code == 200


# --- AC4: cross-site request protection ---------------------------------------------------------------------------------
@pytest.mark.raw_csrf
def test_state_changing_requests_with_a_session_need_the_csrf_token(ctx, admin):
    client = make_client(ctx)
    r = sign_in(client, admin)
    token = client.cookies.get("aiflow4_csrf")
    setup = client.post("/v1/security/mfa/setup", headers={"X-CSRF-Token": token})
    secret = setup.json()["secret"]
    enabled = client.post("/v1/security/mfa/enable", json={"code": mfa.totp_code(secret)},
                          headers={"X-CSRF-Token": token})
    assert enabled.status_code == 200
    cookie = client.cookies.get(COOKIE_NAME)
    token = client.cookies.get("aiflow4_csrf")
    assert token == csrf_token(cookie, SECRET) and "httponly" not in [p.strip().lower() for p in r.headers.get_list("set-cookie")[1].split(";")]
    body = {"session_ttl_s": 7200}
    missing = client.put("/v1/security/settings", json=body)
    assert missing.status_code == 403 and "CSRF" in missing.json()["detail"]
    assert client.put("/v1/security/settings", json=body, headers={"X-CSRF-Token": "x" * 40}).status_code == 403
    assert client.put("/v1/security/settings", json=body, headers={"X-CSRF-Token": token}).status_code == 200
    assert client.get("/v1/security/settings").status_code == 200  # reading needs no token
    # a token of another session does not work
    other = make_client(ctx)
    assert sign_in(other, admin, code=mfa.totp_code(secret, time.time() + 30)).status_code == 200
    assert client.put("/v1/security/settings", json=body, headers={"X-CSRF-Token": other.cookies.get("aiflow4_csrf")}).status_code == 403
    assert client.post("/v1/auth/logout").status_code == 403
    assert ctx.db.one("SELECT value FROM security_settings WHERE key='session_ttl_s'")["value"] == "7200"


def test_login_rejects_cross_site_pre_authentication_posts(ctx, admin):
    client = make_client(ctx)
    rejected = client.post("/v1/auth/login", json={"email": ADMIN, "password": admin},
                           headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"})
    assert rejected.status_code == 403 and "cross-site" in rejected.json()["detail"]
    same_site = client.post("/v1/auth/login", json={"email": ADMIN, "password": admin},
                            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert same_site.status_code == 200


@pytest.mark.raw_csrf
def test_requests_without_a_session_cookie_or_with_a_bearer_token_are_not_blocked_by_csrf(ctx):
    client = make_client(ctx)
    r = client.post("/v1/nodes/join-tokens/inspect", json={"join_token": "x"})
    assert r.status_code == 403 and "CSRF" not in r.json()["detail"]  # the join token is wrong, not the CSRF token
    client.cookies.set(COOKIE_NAME, issue_session("a@example.test", "owner", 4_000_000_000, SECRET))
    bearer = client.post("/v1/nodes/join-tokens/inspect", json={"join_token": "x"}, headers={"Authorization": "Bearer abc"})
    assert "CSRF" not in bearer.json()["detail"]
    assert "CSRF" in client.post("/v1/nodes/join-tokens/inspect", json={"join_token": "x"}).json()["detail"]


def test_console_script_sends_the_token_and_escapes_text():
    from pathlib import Path
    html = (Path(__file__).parents[2] / "aiflow/v4/control/ui/console.html").read_text()
    assert "X-CSRF-Token" in html and "aiflow4_csrf" in html and "renderSecurity" in html
    # every dynamic value in the security screen goes through esc() or Number()
    block = html[html.index("function renderSecurity"):html.index("function secDo")]
    for expr in re.findall(r"'\s*\+\s*([^+;]+?)\s*\+\s*'", block):
        expr = expr.strip()
        assert expr.startswith(("esc(", "Number(", "(")) or expr in {"SEC.busy", "u.disabled ? 'disabled'"} or "'" in expr, expr


def test_security_headers_and_csp(ctx):
    from aiflow.v4.control.ui import mount_ui
    client = make_client(ctx)
    mount_ui(client.app, ctx)
    page = client.get("/security")
    assert page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


# --- AC7: audit log ---------------------------------------------------------------------------------------------------------
def test_people_roles_and_credentials_changes_are_recorded(ctx):
    pw = login.seed_user(ctx.db, ADMIN, "super_admin")
    login.reset_password(ctx.db, ADMIN)
    actions = {(r["category"], r["action"]) for r in ctx.db.all("SELECT category, action FROM audit_log")}
    assert {("people", "user_created"), ("roles", "role_granted"), ("people", "password_reset")} <= actions
    client = make_client(ctx)
    password = login.reset_password(ctx.db, ADMIN)
    secret, _ = enrol(client, password)
    client.put("/v1/security/settings", json={"session_ttl_s": 7200, "require_mfa": True})
    changed = ctx.db.all("SELECT action, actor, detail FROM audit_log WHERE category='settings'")
    assert {c["action"] for c in changed} == {"security_setting_session_ttl_s", "security_setting_require_mfa"}
    assert all(c["actor"] == ADMIN for c in changed)
    # Other console mutations are recorded before dispatch, in the same database transaction as the change.
    client.put("/v1/master/autonomy/budget", json={"daily_budget_minutes": 12})
    entry = ctx.db.one("SELECT actor, category, action, detail FROM audit_log WHERE action LIKE 'PUT %budget'")
    assert entry and entry["actor"] == ADMIN and entry["category"] == "settings" and entry["detail"] == {}
    assert audit.category_for("POST", "/v1/integrations/jira/api-token") == "credentials"


def test_audit_append_failure_rolls_back_the_associated_setting_change(ctx, admin, monkeypatch):
    signed_in = make_client(ctx)
    enrol(signed_in, admin)
    client = TestClient(signed_in.app, follow_redirects=False, raise_server_exceptions=False)
    client.cookies.update(signed_in.cookies)

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(audit, "record", fail_append)
    response = client.put("/v1/security/settings", json={"session_ttl_s": 7200})
    assert response.status_code == 500
    assert ctx.db.one("SELECT value FROM security_settings WHERE key='session_ttl_s'") is None
    # A generic mutation is not dispatched when its pre-mutation audit append fails.
    response = client.put("/v1/master/autonomy/budget", json={"daily_budget_minutes": 12})
    assert response.status_code == 500
    assert ctx.db.one("SELECT daily_budget_minutes FROM master_settings WHERE singleton=true")[
        "daily_budget_minutes"] == 60


def test_secrets_never_reach_the_audit_log(ctx, key, admin):
    secret, codes = enrol(make_client(ctx), admin)
    dump = json.dumps(ctx.db.all("SELECT * FROM audit_log"), default=str)
    assert secret not in dump and admin not in dump and all(c not in dump for c in codes)


def test_the_audit_log_is_read_only_in_the_console_and_in_the_database(ctx, admin):
    client = make_client(ctx)
    enrol(client, admin)
    listed = client.get("/v1/security/audit").json()
    assert listed["entries"] and {"id", "ts", "actor", "category", "action"} <= set(listed["entries"][0])
    assert client.get("/v1/security/audit?category=people").json()["entries"]
    assert client.get("/v1/security/audit?category=bogus").status_code == 400
    # no route can write, change or delete it
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/v1/security/audit").status_code == 405
        assert getattr(client, method)("/v1/security/audit/1").status_code in (404, 405)
    app_routes = [(r.path, sorted(r.methods)) for r in client.app.routes if "audit" in getattr(r, "path", "")]
    assert app_routes == [("/v1/security/audit", ["GET"])]
    # and the database refuses even a direct change
    import psycopg
    with pytest.raises(psycopg.Error):
        ctx.db.run("UPDATE audit_log SET actor='someone else'")
    with pytest.raises(psycopg.Error):
        ctx.db.run("DELETE FROM audit_log")
    with pytest.raises(psycopg.Error):
        ctx.db.run("TRUNCATE audit_log")
    assert ctx.db.one("SELECT count(*) AS n FROM audit_log")["n"] > 0
