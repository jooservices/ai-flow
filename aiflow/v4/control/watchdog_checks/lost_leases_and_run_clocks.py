"""Watchdog check: a run with a lost lease, and the warn and hard work clocks."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...core import work_limits
from .. import merge_flow
from .. import run_inbox as run_inbox_lifecycle
from ..application_context import ApplicationContext
from ..incidents import close_incident, open_incident
from ..repositories.watchdog import (
    ACCOUNT_IDLE_IF_NO_OTHER_RUN,
    LEASE_LOST_AUDIT,
    OPEN_RUN_CLOCK_INCIDENTS,
    RUNNING_RUNS,
    RUNNING_TICKET,
    TICKET_STATUS_TO_DO,
)

log = logging.getLogger("aiflow4.loops")


def lost_leases_and_run_clocks(ctx: ApplicationContext) -> None:
    now = datetime.now(timezone.utc)
    # runs whose lease disappeared (worker died, node down, network split)
    for r in ctx.db.all(RUNNING_RUNS):
        holder = ctx.leases.holder(r["ticket_key"])
        if holder != f"{r['id']}:{r['fence']}":
            lost, _undelivered = run_inbox_lifecycle.watchdog_lost(ctx, r)
            if not lost:
                continue
            ctx.db.run(ACCOUNT_IDLE_IF_NO_OTHER_RUN,
                       (r["account_id"], r["account_id"], r["id"]))
            ctx.events.emit("run.lost", ticket=r["ticket_key"], run_id=str(r["id"]), fence=r["fence"], account=r["account_id"],
                            node=r["node"], reason="lease expired")
            if ctx.jira and not holder:
                try:
                    st = ctx.jira.status(r["ticket_key"])
                    if st == "In Progress":
                        ctx.jira.transition_to(r["ticket_key"], "To Do")
                        ctx.jira.comment(r["ticket_key"], f"AI Flow: the run on {r['node']} ({r['account_id']}) stopped"
                                         " sending heartbeats; the ticket is back in To Do and keeps its branch for the next run.")
                        ctx.db.run(LEASE_LOST_AUDIT,
                                   (r["ticket_key"], str(r["id"]), r["fence"]))
                        ctx.db.run(TICKET_STATUS_TO_DO, (r["ticket_key"],))
                        merge_flow.record_move(ctx, r["ticket_key"], "In Progress", "To Do", merge_flow.FLOW,
                                               f"watchdog: the run on {r['node']} lost its lease")
                        ctx.events.emit("jira.transition", ticket=r["ticket_key"], run_id=str(r["id"]), fence=r["fence"],
                                        account="control", node=r["node"], outcome="To Do", reason="lease lost")
                except Exception as e:  # noqa: BLE001 - lease requeue failure must not stop this check
                    log.warning("could not requeue %s: %s", r["ticket_key"], e)
            continue
        # clocks: 30 target, 45 warn, 60 hard (per project)
        p = ctx.repo().projects.get(r["project"])
        wm = p.work_minutes() if p else dict(work_limits.DEFAULTS)
        minutes = (now - r["started_at"]).total_seconds() / 60
        if minutes >= wm["hard"]:
            open_incident(ctx, "run_hard_limit", r["ticket_key"], "critical", f"{r['ticket_key']} passed {wm['hard']} minutes",
                          f"Run {r['id']} on {r['node']} ({r['account_id']}) has worked {int(minutes)} minutes.")
        elif minutes >= wm["warn"]:
            open_incident(ctx, "run_slow", r["ticket_key"], "warning", f"{r['ticket_key']} passed {wm['warn']} minutes",
                          f"Run {r['id']} on {r['node']} ({r['account_id']}) has worked {int(minutes)} minutes.")
    for kind in ("run_slow", "run_hard_limit"):
        for i in ctx.db.all(OPEN_RUN_CLOCK_INCIDENTS, (kind,)):
            if not ctx.db.one(RUNNING_TICKET, (i["subject"],)):
                close_incident(ctx, kind, i["subject"], "run finished")
