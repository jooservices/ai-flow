"""Persistence queries for account state shared by candidate selection and control services."""
from __future__ import annotations


class AccountsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def master_paused(self, account_id: str) -> bool:
        """Whether the master has an active temporary pause for this account."""
        return bool(self.db.one("SELECT 1 AS paused FROM master_account_pauses"
                                " WHERE account_id=%s AND paused_until > now()", (account_id,)))
