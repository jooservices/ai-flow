"""Watchdog check: repeated dev or verify failures open a ticket_stuck incident."""
from __future__ import annotations

from ..application_context import ApplicationContext
from ..incidents import blocked_handoff_tickets, close_incident, open_incident
from ..repositories.watchdog import LATEST_RUN_SUMMARY, OPEN_STUCK_INCIDENTS, STUCK_STREAK_SELECT


def stuck_tickets(ctx: ApplicationContext) -> None:
    # tickets whose dev runs keep failing (backoff in the API keeps them from spinning; a person should look)
    from ..features.work.constants import BACKOFF_SQL
    streak_sql = BACKOFF_SQL.split(STUCK_STREAK_SELECT)[0] + STUCK_STREAK_SELECT
    stuck: dict[str, int] = {}
    for step in ("dev", "verify"):
        for r in ctx.db.all(streak_sql, {"step": step}):
            stuck[r["ticket_key"]] = max(stuck.get(r["ticket_key"], 0), r["fails"])
    for key, fails in stuck.items():
        if fails >= 3:
            last = ctx.db.one(LATEST_RUN_SUMMARY, (key,))
            open_incident(ctx, "ticket_stuck", key, "warning", f"{key} failed {fails} runs in a row",
                          f"Last error ({last['account_id'] if last else '?'}): {(last['summary'] if last else '')[:300]}."
                          " Retries wait up to 30 minutes; fix the cause or put the ticket on hold.")
    blocked = blocked_handoff_tickets(ctx)
    for i in ctx.db.all(OPEN_STUCK_INCIDENTS):
        if i["subject"].startswith("tool_wait:"):
            continue  # tooling.check_stalled_waits owns the two-hour missing-requirement warning lifecycle
        if i["subject"] in blocked:
            continue
        if stuck.get(i["subject"], 0) < 3:
            close_incident(ctx, "ticket_stuck", i["subject"], "a run produced work again")
