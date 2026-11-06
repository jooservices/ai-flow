"""Watchdog check: an In Progress ticket with no run goes back to To Do."""
from __future__ import annotations

import logging

from .. import merge_flow
from ..application_context import ApplicationContext
from ..repositories.watchdog import ORPHAN_AUDIT, ORPHAN_TICKETS, TICKET_STATUS_TO_DO

log = logging.getLogger("aiflow4.loops")


def orphaned_tickets(ctx: ApplicationContext) -> None:
    # orphans: Jira says In Progress but no run is working on it (a worker died between writes, a deploy, a manual
    # move). After 10 quiet minutes the ticket goes back to To Do so it is never abandoned.
    if ctx.jira:
        for t in ctx.db.all(ORPHAN_TICKETS):
            if ctx.leases.holder(t["key"]):
                continue
            try:
                if ctx.jira.status(t["key"]) != "In Progress":
                    continue
                ctx.jira.transition_to(t["key"], "To Do")
                ctx.jira.comment(t["key"], "AI Flow · watchdog · requeued\nWhat: The ticket was In Progress but no run had worked on it"
                                 " for 10 minutes.\nWhy: A worker stopped between two updates, so nobody owned the ticket.\n"
                                 "Next: Back to To Do; the next worker continues its branch.")
                ctx.db.run(TICKET_STATUS_TO_DO, (t["key"],))
                merge_flow.record_move(ctx, t["key"], "In Progress", "To Do", merge_flow.FLOW,
                                       "watchdog: In Progress without a run for 10 minutes")
                ctx.db.run(ORPHAN_AUDIT, (t["key"],))
                ctx.events.emit("jira.transition", ticket=t["key"], account="control", outcome="To Do",
                                reason="In Progress without a run for 10 minutes")
                ctx.events.emit("ticket.requeued", ticket=t["key"], reason="In Progress without a run for 10 minutes")
            except Exception as e:  # noqa: BLE001 - orphan requeue failure must not stop this check
                log.warning("could not requeue orphan %s: %s", t["key"], e)
