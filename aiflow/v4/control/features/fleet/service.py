"""Policy for the provider sign-in link of a managed AI account: a link is valid for a fixed time."""
from __future__ import annotations

from datetime import UTC, datetime

from . import repository


class SignInRefused(Exception):
    """A sign-in request that cannot be served; the route turns it into the HTTP answer with the same status."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def link_expired(expires_at: datetime | None, url: str) -> bool:
    """A link past its expiry is expired; a stored link without any expiry (stored before expiry existed) is too."""
    if not url:
        return False
    return expires_at is None or expires_at <= datetime.now(UTC)


def sign_in_details(db, account_id: str) -> dict:
    row = repository.active_sign_in_link(db, account_id)
    if not row:
        raise SignInRefused(404, "no active sign-in link for this account")
    if link_expired(row["sign_in_expires_at"], row["sign_in_url"]):
        raise SignInRefused(410, "provider sign-in link has expired; start a new sign-in")
    return {"account": account_id, "node": row["node"], "provider": row["provider"],
            "status": row["sign_in_status"], "sign_in_url": row["sign_in_url"],
            "code": row["sign_in_code"], "error": row["sign_in_error"],
            "expires_at": row["sign_in_expires_at"].isoformat() if row["sign_in_expires_at"] else None}


def check_status_report(db, account_id: str, node: str, request_id: str, status: str, url: str) -> None:
    """Refuse a report for another node, a replaced request, or a waiting report of an expired link."""
    row = repository.sign_in_request(db, account_id)
    if not row or row["node"] != node:
        raise SignInRefused(404, "account sign-in request is not assigned to this machine")
    if row["sign_in_request_id"] != request_id:
        raise SignInRefused(409, "sign-in request has been replaced")
    same_link = not url or url == row["sign_in_url"]  # a different link is a new one and starts a new lifetime
    if status == "waiting" and same_link and row["sign_in_expires_at"] is not None \
            and link_expired(row["sign_in_expires_at"], row["sign_in_url"]):
        raise SignInRefused(409, "provider sign-in link has expired; start a new sign-in")
