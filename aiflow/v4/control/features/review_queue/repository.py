"""Named SQL reads for human-review run history."""
from __future__ import annotations

from typing import Any

from .models import ReviewQueueDevRun, ReviewQueueLatestRun, ReviewQueueVerifyRun


class ReviewQueueRepository:
    """Read the selected run history through the caller's database context."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def latest_pr_runs(self, selected_keys: list[str]) -> list[ReviewQueueDevRun]:
        sql = ("SELECT DISTINCT ON (ticket_key) ticket_key,pr_url,summary,details FROM runs"
               " WHERE ticket_key=ANY(%s) AND pr_url<>'' ORDER BY ticket_key,started_at DESC")
        return [ReviewQueueDevRun.from_row(row) for row in self.db.all(sql, (selected_keys,))]

    def latest_runs(self, selected_keys: list[str]) -> list[ReviewQueueLatestRun]:
        sql = ("SELECT DISTINCT ON (ticket_key) ticket_key,step,outcome,summary,details,started_at FROM runs"
               " WHERE ticket_key=ANY(%s) ORDER BY ticket_key,started_at DESC")
        return [ReviewQueueLatestRun.from_row(row) for row in self.db.all(sql, (selected_keys,))]

    def latest_verify_runs(self, selected_keys: list[str]) -> list[ReviewQueueVerifyRun]:
        sql = ("SELECT DISTINCT ON (ticket_key) ticket_key,outcome,summary,details,started_at FROM runs"
               " WHERE ticket_key=ANY(%s) AND step='verify' ORDER BY ticket_key,started_at DESC")
        return [ReviewQueueVerifyRun.from_row(row) for row in self.db.all(sql, (selected_keys,))]
