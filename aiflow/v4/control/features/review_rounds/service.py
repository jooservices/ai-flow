"""Review-round Dashboard query and aggregation."""
from __future__ import annotations

from typing import Any

from ....core.review_findings import legacy_findings
from .models import ReviewRun
from .repository import ReviewRoundsRepository


def review_round_metrics(repository: ReviewRoundsRepository,
                         projects: list[str] | None = None) -> dict[str, Any]:
    """Return merged-ticket review rounds and finding counts within the requested project scope."""
    return calculate_review_round_metrics(repository.merged_reviews(projects))


def calculate_review_round_metrics(rows: list[ReviewRun]) -> dict[str, Any]:
    """Group review counts by merged ticket and calculate the two-round target average."""
    grouped: dict[str, list[dict[str, int]]] = {}
    for row in rows:
        ticket = row.ticket_key
        if not ticket:
            continue
        details = row.details
        reviews = grouped.setdefault(ticket, [])
        round_number = int(details.get("review_round") or len(reviews) + 1)
        reviews.append(_review_counts(details, round_number))
    tickets = [{"key": key, "rounds": len(reviews), "reviews": reviews} for key, reviews in grouped.items()]
    rounds = [ticket["rounds"] for ticket in tickets]
    average = round(sum(rounds) / len(rounds), 2) if rounds else None
    return {"target_rounds": 2, "merged_tickets": len(tickets), "average_rounds": average, "tickets": tickets}


def _review_counts(details: dict[str, Any], round_number: int) -> dict[str, int]:
    history = details.get("review_findings")
    if isinstance(history, list):
        active = [item for item in history if isinstance(item, dict)
                  and item.get("status") in {"new", "not_fixed"}]
        findings = len(active)
        new_findings = sum(item.get("status") == "new" for item in active) if round_number > 1 else 0
    else:
        findings = len(legacy_findings(str(details.get("findings") or "")))
        new_findings = 0
    return {"round": round_number, "findings": findings, "new_findings": new_findings}
