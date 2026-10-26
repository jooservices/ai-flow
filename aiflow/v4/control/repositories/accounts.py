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

    def capacity_dashboard_accounts(self) -> list[dict]:
        """Account quota and role fields displayed by the capacity dashboard."""
        return self.db.all("SELECT id,provider,tier,roles,quota FROM accounts ORDER BY id")

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

    def dashboard_accounts(self) -> list[dict]:
        """Account rows for Command Center account summaries."""
        return self.db.all("SELECT * FROM accounts ORDER BY node, provider, id")

    def dashboard_sign_in_statuses(self) -> dict[str, str]:
        """Non-idle sign-in statuses shown beside each managed account."""
        return {row["id"]: row["sign_in_status"] for row in self.db.all(
            "SELECT id,sign_in_status FROM managed_ai_accounts WHERE removed_at IS NULL AND sign_in_status<>'idle'")}

    def dashboard_managed_ids(self) -> set[str]:
        """Accounts whose machine assignments are managed from Command Center."""
        return {row["id"] for row in self.db.all("SELECT id FROM shared_accounts WHERE managed")}

    def removed_managed_ids(self) -> set[str]:
        """Managed accounts removed from the live registry view."""
        return {row["id"] for row in self.db.all(
            "SELECT id FROM managed_ai_accounts WHERE removed_at IS NOT NULL")}

    def managed_machine_accounts(self) -> list[dict]:
        """Active accounts created by the machine account management screen."""
        return self.db.all("SELECT * FROM managed_ai_accounts WHERE removed_at IS NULL ORDER BY created_at,id")
