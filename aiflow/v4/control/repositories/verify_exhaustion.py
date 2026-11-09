"""Reads behind the `verify_no_verdict` incident: open incidents, tickets in verification and account facts."""
from __future__ import annotations

from typing import Any

KIND = "verify_no_verdict"


def open_subjects(db: Any) -> set[str]:
    """Ticket keys with an open `verify_no_verdict` incident."""
    return {row["subject"] for row in db.all(
        "SELECT subject FROM incidents WHERE kind=%s AND closed_at IS NULL", (KIND,))}


def tickets_in_verify(db: Any) -> list[dict]:
    """Non-epic tickets currently waiting in verification."""
    return db.all("SELECT key, labels, type, project FROM tickets WHERE status='In Verify'"
                  " AND lower(coalesce(type,'')) <> 'epic'")


def last_dev_account(db: Any, ticket_key: str) -> str:
    """Account that produced the latest finished development run for the ticket."""
    row = db.one("SELECT account_id FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='done'"
                 " ORDER BY started_at DESC LIMIT 1", (ticket_key,))
    return (row or {}).get("account_id", "")


def account_master_paused(db: Any, account_id: str) -> bool:
    return bool(db.one("SELECT 1 AS paused FROM master_account_pauses WHERE account_id=%s AND paused_until > now()",
                       (account_id,)))


def quota_at_risk(db: Any, account_id: str) -> bool:
    return bool((db.one("SELECT quota_at_risk FROM accounts WHERE id=%s", (account_id,)) or {}).get("quota_at_risk"))
