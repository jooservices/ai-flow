"""SQL read model for completed reviews of merged tickets."""
from __future__ import annotations

from typing import Any

from .models import ReviewRun


class ReviewRoundsRepository:
    def __init__(self, db: Any) -> None:
        self.db = db

    def merged_reviews(self, projects: list[str] | None = None) -> list[ReviewRun]:
        """Load completed review runs for merged tickets in the optional project scope."""
        scope = " AND t.project=ANY(%s)" if projects is not None else ""
        params = (projects,) if projects is not None else ()
        rows = self.db.all(
            "SELECT r.ticket_key,r.details FROM runs r JOIN tickets t ON t.key=r.ticket_key"
            " WHERE r.step='verify' AND r.status='finished' AND t.status='Done' AND t.pr_state='merged'"
            " AND r.details->>'verdict' IN ('PASS','FAIL')" + scope
            + " ORDER BY r.ticket_key,r.started_at,r.id", params)
        return [ReviewRun.from_row(row) for row in rows]
