"""Persistence of the provider sign-in link of a managed AI account."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

SIGN_IN_LINK_LIFETIME = timedelta(minutes=15)


def active_sign_in_link(db: Any, account_id: str) -> dict | None:
    """The sign-in request of an account that is not idle, with the link and its expiry."""
    return db.one("SELECT node,provider,sign_in_status,sign_in_url,sign_in_code,sign_in_error,"
                  "sign_in_expires_at FROM managed_ai_accounts"
                  " WHERE id=%s AND removed_at IS NULL AND sign_in_status<>'idle'", (account_id,))


def sign_in_request(db: Any, account_id: str) -> dict | None:
    """The node and request an account sign-in is assigned to."""
    return db.one("SELECT node,sign_in_request_id,sign_in_status,sign_in_url,sign_in_expires_at FROM managed_ai_accounts"
                  " WHERE id=%s AND removed_at IS NULL", (account_id,))


def apply_sign_in_status(connection: Any, account_id: str, node: str, request_id: str, status: str,
                         url: str, code: str, error: str) -> bool:
    """Store a node's status report. A waiting report is refused when the stored link has expired; a new or legacy link
    (no expiry yet) starts a fresh lifetime, so a repeated report never keeps a link without an expiry."""
    row = connection.execute(
        "UPDATE managed_ai_accounts SET sign_in_status=%s,"
        "sign_in_url=CASE WHEN %s<>'' THEN %s ELSE sign_in_url END,"
        "sign_in_code=CASE WHEN %s<>'' THEN %s ELSE sign_in_code END,"
        "sign_in_expires_at=CASE WHEN %s<>'' AND (%s<>sign_in_url OR sign_in_expires_at IS NULL)"
        " THEN now()+%s ELSE sign_in_expires_at END,"
        "sign_in_error=%s,updated_at=now() WHERE id=%s AND node=%s AND sign_in_request_id=%s"
        " AND removed_at IS NULL"
        " AND NOT (%s='waiting' AND (%s='' OR %s=sign_in_url) AND sign_in_url<>'' AND sign_in_expires_at IS NOT NULL"
        " AND sign_in_expires_at<=now()) RETURNING id",
        (status, url, url, code, code, url, url, SIGN_IN_LINK_LIFETIME, error[:300], account_id, node, request_id,
         status, url, url)).fetchone()
    return bool(row)
