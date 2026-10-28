"""Database-backed, account-level scheduling for no-generation CLI quota reads."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from . import quota as quota_reporting
from .db import J

DEFAULT_INTERVAL_MINUTES = 15
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 1440
LEASE_SECONDS = 180


def get_settings(db) -> dict[str, Any]:
    row = db.one("SELECT interval_minutes,updated_by,updated_at FROM quota_refresh_settings WHERE id=true")
    if not row:
        return {"interval_minutes": DEFAULT_INTERVAL_MINUTES, "updated_by": "", "updated_at": None,
                "defaults": {"interval_minutes": DEFAULT_INTERVAL_MINUTES}}
    return {"interval_minutes": int(row["interval_minutes"]), "updated_by": row["updated_by"],
            "updated_at": row["updated_at"].isoformat(),
            "defaults": {"interval_minutes": DEFAULT_INTERVAL_MINUTES}}


def set_settings(db, interval_minutes: int, by: str) -> dict[str, Any]:
    if not MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES:
        raise ValueError(f"interval must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES} minutes")
    db.run("INSERT INTO quota_refresh_settings (id,interval_minutes,updated_by) VALUES (true,%s,%s)"
           " ON CONFLICT (id) DO UPDATE SET interval_minutes=EXCLUDED.interval_minutes,"
           " updated_by=EXCLUDED.updated_by,updated_at=now()", (interval_minutes, by[:200]))
    db.run("UPDATE quota_refresh_jobs SET next_due_at=CASE WHEN last_attempt_at IS NULL THEN now()"
           " ELSE last_attempt_at+make_interval(mins => %s) END WHERE lease_token IS NULL", (interval_minutes,))
    return get_settings(db)


def on_sign_in_completed(connection, account_id: str, provider: str) -> None:
    """Queue a fresh quota read; record auth health for providers without a read-only quota command."""
    connection.execute("INSERT INTO quota_refresh_jobs (account_id,next_due_at) VALUES (%s,now())"
                       " ON CONFLICT (account_id) DO UPDATE SET next_due_at=now()", (account_id,))
    if provider not in {"codex", "claude"}:
        connection.execute(
            "UPDATE accounts SET last_probe_at=now(),status=CASE WHEN status='signed_out' THEN 'idle'"
            " ELSE status END,last_error=CASE WHEN status='signed_out' THEN '' ELSE last_error END,"
            "updated_at=now() WHERE id=%s", (account_id,))


def claim(db, account_id: str, node: str) -> dict | None:
    """Atomically lease one due account refresh, even when several nodes ask at once."""
    db.run("INSERT INTO quota_refresh_jobs (account_id) VALUES (%s) ON CONFLICT (account_id) DO NOTHING",
           (account_id,))
    token = uuid.uuid4()
    return db.one(
        "UPDATE quota_refresh_jobs SET lease_token=%s,lease_owner=%s,"
        " lease_expires_at=now()+make_interval(secs => %s),last_attempt_at=now()"
        " WHERE account_id=%s AND next_due_at<=now()"
        " AND (lease_expires_at IS NULL OR lease_expires_at<=now())"
        " AND NOT EXISTS (SELECT 1 FROM managed_ai_accounts m WHERE m.id=quota_refresh_jobs.account_id"
        " AND m.removed_at IS NULL AND m.sign_in_status IN ('queued','starting','waiting'))"
        " RETURNING lease_token,lease_expires_at,last_attempt_at AS claimed_at",
        (token, node[:200], LEASE_SECONDS, account_id))


def finish(db, account_id: str, lease_token: str, *, ok: bool, error: str = "", source: str = "cli",
           quota: dict | None = None, model: str = "", observed_at: datetime | None = None) -> bool:
    """Record a quota observation only when the current claim still owns its account lease."""
    with db.pool.connection() as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + account_id,))
        row = connection.execute(
            "SELECT interval_minutes FROM quota_refresh_settings WHERE id=true").fetchone()
        interval = int(row["interval_minutes"]) if row else DEFAULT_INTERVAL_MINUTES
        job = connection.execute("SELECT last_attempt_at,lease_owner FROM quota_refresh_jobs"
                                 " WHERE account_id=%s AND lease_token=%s FOR UPDATE",
                                 (account_id, uuid.UUID(str(lease_token)))).fetchone()
        if not job:
            return False
        managed = connection.execute(
            "SELECT sign_in_status,sign_in_started_at FROM managed_ai_accounts"
            " WHERE id=%s AND removed_at IS NULL", (account_id,)).fetchone()
        signin_check = bool(managed and managed["sign_in_status"] == "completed"
                            and managed["sign_in_started_at"]
                            and job["last_attempt_at"] >= managed["sign_in_started_at"])
        retry_after_signin = bool(managed and managed["sign_in_status"] == "completed"
                                  and managed["sign_in_started_at"]
                                  and job["last_attempt_at"] < managed["sign_in_started_at"])
        received_at = connection.execute("SELECT clock_timestamp() AS received_at").fetchone()["received_at"]
        observed_at = observed_at or received_at
        observed_at = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at.astimezone(UTC)
        observed_at = min(received_at, max(job["last_attempt_at"], observed_at))
        if ok and quota:
            account = connection.execute("SELECT provider,quota_observed_at FROM accounts WHERE id=%s",
                                          (account_id,)).fetchone()
            if not account:
                return False
            superseded = bool(account["quota_observed_at"] and account["quota_observed_at"] > observed_at)
            connection.execute(
                "INSERT INTO account_quota_samples (account_id,provider,model,sampled_at,observed_at,source,quota)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (account_id, account["provider"], model[:200], observed_at,
                 observed_at, source, J(quota)))
            if not superseded:
                connection.execute(
                    "UPDATE accounts SET quota=%s,quota_source=%s,quota_observed_at=%s,updated_at=now()"
                    " WHERE id=%s AND (quota_observed_at IS NULL OR quota_observed_at<=%s)",
                    (J(quota), source, observed_at, account_id, observed_at))
            current = connection.execute("SELECT provider,quota FROM accounts WHERE id=%s", (account_id,)).fetchone()
            forecast = quota_reporting.account_assessment_in(
                connection, account_id, current["provider"], current["quota"])
            connection.execute("UPDATE accounts SET quota_at_risk=%s,quota_forecast=%s WHERE id=%s",
                               (forecast["at_risk"], J(forecast), account_id))
            # Only a status read started after a managed login can satisfy its existing completion gate.
            # Scheduled quota refreshes never clear task quota cooldowns or rewrite general account health.
            if signin_check:
                connection.execute(
                    "UPDATE accounts SET last_probe_at=greatest(coalesce(last_probe_at,'-infinity'::timestamptz),%s),"
                    " status=CASE WHEN status='signed_out' THEN 'idle' ELSE status END,"
                    " last_error=CASE WHEN status='signed_out' THEN '' ELSE last_error END,updated_at=now()"
                    " WHERE id=%s", (observed_at, account_id))
        updated = connection.execute(
            "UPDATE quota_refresh_jobs SET next_due_at=CASE WHEN %s THEN now()"
            " ELSE now()+make_interval(mins => %s) END,"
            " lease_token=NULL,lease_owner='',lease_expires_at=NULL,last_success_at=CASE WHEN %s THEN now()"
            " ELSE last_success_at END,last_error=%s,last_source=%s,last_observed_at=CASE WHEN %s THEN %s"
            " ELSE last_observed_at END WHERE account_id=%s AND lease_token=%s",
            (retry_after_signin, interval, bool(ok and quota), "" if ok else error[:300], source if ok and quota else "",
             bool(ok and quota), observed_at, account_id, lease_token))
        return updated.rowcount == 1


def record_run_result(db, run: dict, body: Any, *, update_account: bool) -> datetime | None:
    """Store run-reported quota with its worker observation time, preserving newer provider snapshots."""
    if not body.quota:
        return None
    account_id = str(run["account_id"])
    with db.pool.connection() as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + account_id,))
        received_at = connection.execute("SELECT clock_timestamp() AS received_at").fetchone()["received_at"]
        started_at = run["started_at"]
        started_at = started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at.astimezone(UTC)
        observed_at = body.quota_observed_at
        if observed_at is None:
            observed_at = started_at
        else:
            observed_at = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at.astimezone(UTC)
            if observed_at < started_at or observed_at > received_at:
                observed_at = started_at
        account = connection.execute("SELECT provider,quota,quota_source,quota_observed_at FROM accounts WHERE id=%s",
                                     (account_id,)).fetchone()
        if not account:
            return None
        duration_ms = max(0, int((observed_at - started_at).total_seconds() * 1000))
        connection.execute(
            "INSERT INTO account_quota_samples (account_id,provider,model,quota,tokens_in,tokens_out,cost_usd,"
            "duration_ms,source,observed_at,sampled_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'run',%s,%s)",
            (account_id, run["provider"], run["model"], J(body.quota), body.tokens_in, body.tokens_out,
             body.cost_usd, duration_ms, observed_at, observed_at))
        updated = False
        if update_account and (account["quota_observed_at"] is None or account["quota_observed_at"] <= observed_at):
            result = connection.execute(
                "UPDATE accounts SET quota=%s,quota_source='run',quota_observed_at=%s,updated_at=now()"
                " WHERE id=%s AND (quota_observed_at IS NULL OR quota_observed_at<=%s)",
                (J(body.quota), observed_at, account_id, observed_at))
            updated = result.rowcount == 1
        if updated:
            current = connection.execute("SELECT provider,quota,quota_source FROM accounts WHERE id=%s",
                                         (account_id,)).fetchone()
            forecast = quota_reporting.account_assessment_in(
                connection, account_id, current["provider"], current["quota"] if current["quota_source"] else {})
            connection.execute("UPDATE accounts SET quota_at_risk=%s,quota_forecast=%s WHERE id=%s",
                               (forecast["at_risk"], J(forecast), account_id))
        return observed_at


def status(db) -> list[dict]:
    return db.all("SELECT j.account_id,a.provider,j.next_due_at,j.last_attempt_at,j.last_success_at,j.last_error,"
                  "j.last_source,j.last_observed_at FROM quota_refresh_jobs j"
                  " JOIN accounts a ON a.id=j.account_id ORDER BY j.account_id")
