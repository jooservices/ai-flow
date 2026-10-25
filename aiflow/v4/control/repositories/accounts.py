"""Persistence queries for account state shared by candidate selection and control services."""
from __future__ import annotations


class AccountsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def master_paused(self, account_id: str) -> bool:
        """Whether the master has an active temporary pause for this account."""
        return bool(self.db.one("SELECT 1 AS paused FROM master_account_pauses"
                                " WHERE account_id=%s AND paused_until > now()", (account_id,)))

    def capacity_routing_accounts(self) -> list[dict]:
        """Account quota and role facts used to compare eligible development candidates."""
        return self.db.all("SELECT id,provider,tier,roles,quota_forecast FROM accounts ORDER BY id")

    def quota_at_risk(self, account_id: str) -> bool:
        """Whether the latest watchdog forecast marks an account as likely to exhaust its quota."""
        row = self.db.one("SELECT quota_at_risk FROM accounts WHERE id=%s", (account_id,))
        return bool(row and row["quota_at_risk"])

    def quota_peer_candidates(self, account_id: str) -> list[dict]:
        """Same-provider account facts eligible for additional routing checks."""
        return self.db.all(
            "SELECT peer.id,peer.roles,peer.status,peer.quota_at_risk FROM accounts source"
            " JOIN accounts peer ON peer.provider=source.provider AND peer.id<>source.id"
            " WHERE source.id=%s AND peer.status NOT IN ('cooldown','signed_out','error') ORDER BY peer.id",
            (account_id,))
