"""All SQL of console sign-in, sessions and the user list: `console_users`, `console_sessions` and the sign-in audit row."""
from __future__ import annotations

from typing import Any

from ...db import J


def clear_expired_lock(db: Any, email: str) -> None:
    """Reset the failure count of an account whose lock has run out."""
    db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, updated_at=now()"
           " WHERE email=%s AND locked_until IS NOT NULL AND locked_until <= now()", (email,))


def find_user(db: Any, email: str) -> dict | None:
    """The sign-in row of a user: password hash, lock, roles and second factor."""
    return db.one("SELECT email, display_name, role, roles, password_hash, disabled, locked_until,"
                  " totp_secret, totp_enabled FROM console_users WHERE email=%s", (email,))


def record_failed_attempt(db: Any, email: str, max_failures: int, lock_seconds: int) -> None:
    """Count a wrong password; the account locks once `max_failures` in a row are reached."""
    db.run("UPDATE console_users SET failed_attempts=failed_attempts+1,"
           " locked_until=CASE WHEN failed_attempts+1 >= %s THEN now() + make_interval(secs => %s)"
           " ELSE locked_until END, updated_at=now() WHERE email=%s", (max_failures, lock_seconds, email))


def record_signed_in(db: Any, email: str) -> None:
    db.run("UPDATE console_users SET failed_attempts=0, locked_until=NULL, last_login_at=now(), updated_at=now()"
           " WHERE email=%s", (email,))


def open_session(db: Any, sid: str, email: str, role: str, ip: str, ttl_seconds: int) -> None:
    db.run("INSERT INTO console_sessions (sid, email, role, ip, expires_at)"
           " VALUES (%s,%s,%s,%s, now() + make_interval(secs => %s))", (sid, email, role, ip, ttl_seconds))


def end_session(db: Any, sid: str, revoked_by: str) -> None:
    db.run("UPDATE console_sessions SET revoked_at=now(), revoked_by=%s WHERE sid=%s AND revoked_at IS NULL",
           (revoked_by, sid))


def session_is_live(db: Any, sid: str) -> bool:
    """True while the session is neither ended nor expired."""
    return db.one("SELECT 1 AS x FROM console_sessions WHERE sid=%s AND revoked_at IS NULL"
                  " AND expires_at > now()", (sid,)) is not None


def display_name(db: Any, email: str) -> str:
    row = db.one("SELECT display_name FROM console_users WHERE email=%s", (email,))
    return (row or {}).get("display_name", "")


def list_users(db: Any) -> list[dict]:
    return db.all("SELECT email,display_name,role,disabled,created_at FROM console_users ORDER BY email")


def count_active_users(db: Any) -> int:
    return int(db.one("SELECT count(*) AS count FROM console_users WHERE disabled=false")["count"])


def write_console_audit(db: Any, action: str, email: str, accepted: bool, result: str) -> None:
    """One `write_audit` row for a console sign-in or user creation."""
    db.run("INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result, payload)"
           " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
           ("console", action, "", "", None, email, accepted, result, J({})))
