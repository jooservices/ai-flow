"""The security screen's sessions, people and read-only audit routes."""
from __future__ import annotations

from fastapi import HTTPException, Request

from . import audit as audit_log
from . import mfa
from .console_auth import ConsoleUser
from .security_guard import SecurityKit

USERS_SQL = (
    "SELECT u.email, u.display_name, u.role, u.disabled, u.totp_enabled, u.last_login_at,"
    " (u.locked_until IS NOT NULL AND u.locked_until > now()) AS locked,"
    " (SELECT count(*) FROM console_recovery_codes r WHERE r.email=u.email AND r.used_at IS NULL) AS recovery_codes_left,"
    " (SELECT count(*) FROM console_sessions s WHERE s.email=u.email AND s.revoked_at IS NULL AND s.expires_at>now()) AS sessions"
    " FROM console_users u ORDER BY u.email")


def active_sessions(kit: SecurityKit, user: ConsoleUser) -> list[dict]:
    rows = kit.ctx.db.all("SELECT sid, email, role, ip, created_at, expires_at FROM console_sessions"
                          " WHERE revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC LIMIT 200")
    for r in rows:
        r["created_at"], r["expires_at"] = r["created_at"].isoformat(), r["expires_at"].isoformat()
        r["current"] = r["sid"] == user.sid
    return rows


def revoke(kit: SecurityKit, request: Request, user: ConsoleUser, where: str, params: tuple, target: str) -> int:
    n = kit.ctx.db.run(f"UPDATE console_sessions SET revoked_at=now(), revoked_by=%s WHERE revoked_at IS NULL AND {where}",
                       (user.subject, *params))
    audit_log.record(kit.ctx.db, user.subject, "security", "forced_sign_out", target, {"sessions": n}, kit.ip_of(request))
    return n


def known_user(kit: SecurityKit, email: str) -> dict:
    row = kit.ctx.db.one("SELECT email, role FROM console_users WHERE email=%s", (email.strip().lower(),))
    if not row:
        raise HTTPException(404, "unknown person")
    return row


def people_rows(kit: SecurityKit) -> list[dict]:
    rows = kit.ctx.db.all(USERS_SQL)
    for r in rows:
        r["last_login_at"] = r["last_login_at"].isoformat() if r["last_login_at"] else None
        r["recovery_codes_left"], r["sessions"] = int(r["recovery_codes_left"]), int(r["sessions"])
    return rows


def install_security_sessions(kit: SecurityKit) -> None:
    app = kit.app

    @app.get("/v1/security/sessions")
    def list_sessions(request: Request) -> dict:
        user = kit.require(request, "security.manage")
        return {"sessions": active_sessions(kit, user)}

    @app.post("/v1/security/sessions/{sid}/revoke")
    def revoke_session(sid: str, request: Request) -> dict:
        user = kit.require(request, "security.manage")
        row = kit.ctx.db.one("SELECT email FROM console_sessions WHERE sid=%s", (sid,))
        if not row:
            raise HTTPException(404, "unknown session")
        return {"revoked": revoke(kit, request, user, "sid=%s", (sid,), row["email"])}


def install_security_people(kit: SecurityKit) -> None:
    app = kit.app

    @app.post("/v1/security/users/{email}/sign-out")
    def sign_out_user(email: str, request: Request) -> dict:
        user = kit.require(request, "security.manage")
        row = known_user(kit, email)
        return {"revoked": revoke(kit, request, user, "email=%s", (row["email"],), row["email"])}

    @app.get("/v1/security/users")
    def list_users(request: Request) -> dict:
        kit.require(request, "security.manage")
        return {"users": people_rows(kit)}

    @app.post("/v1/security/users/{email}/mfa/reset")
    def reset_user_mfa(email: str, request: Request) -> dict:
        """A person lost the authenticator and every recovery code: an authorised user clears their second factor."""
        user = kit.require(request, "security.manage")
        row = known_user(kit, email)
        mfa.clear(kit.ctx.db, row["email"])
        revoke(kit, request, user, "email=%s", (row["email"],), row["email"])
        audit_log.record(kit.ctx.db, user.subject, "people", "mfa_reset", row["email"], {}, kit.ip_of(request))
        return {"reset": True}

    @app.post("/v1/security/users/{email}/unlock")
    def unlock_user(email: str, request: Request) -> dict:
        user = kit.require(request, "security.manage")
        row = known_user(kit, email)
        kit.ctx.db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, updated_at=now() WHERE email=%s",
                       (row["email"],))
        audit_log.record(kit.ctx.db, user.subject, "people", "account_unlocked", row["email"], {}, kit.ip_of(request))
        return {"unlocked": True}


def install_security_audit(kit: SecurityKit) -> None:
    @kit.app.get("/v1/security/audit")
    def read_audit(request: Request, category: str = "", actor: str = "", before: int = 0, limit: int = 100) -> dict:
        kit.require(request, "security.audit")
        if category and category not in audit_log.CATEGORIES:
            raise HTTPException(400, f"category must be one of {', '.join(audit_log.CATEGORIES)}")
        return {"entries": audit_log.listing(kit.ctx.db, category, actor, before, limit),
                "categories": list(audit_log.CATEGORIES)}
