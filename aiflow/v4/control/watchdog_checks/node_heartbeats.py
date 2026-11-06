"""Watchdog check: mark a node offline when its heartbeat has stopped."""
from __future__ import annotations

from datetime import datetime, timezone

from .. import system_check
from ..application_context import ApplicationContext
from ..incidents import close_incident, open_incident
from ..repositories.watchdog import NODE_MARK_OFFLINE, NODES_HEARTBEAT
from ..state import NODE_OFFLINE_S


def node_heartbeats(ctx: ApplicationContext) -> None:
    now = datetime.now(timezone.utc)
    # nodes
    for n in ctx.db.all(NODES_HEARTBEAT):
        age = None if n["last_heartbeat"] is None else (now - n["last_heartbeat"]).total_seconds()
        if n["status"] == "online" and (age is None or age > NODE_OFFLINE_S):
            ctx.db.run(NODE_MARK_OFFLINE, (n["name"],))
            ctx.events.emit("node.offline", node=n["name"], reason=f"no heartbeat for {int(age or 0)} s")
            open_incident(ctx, "node_down", n["name"], "critical", f"node {n['name']} is down",
                          f"No heartbeat for {int(age or 0)} seconds. Its runs lose their lease after 90 s and go back to To Do.")
            system_check.refresh_node(ctx, n["name"], "heartbeat_lost")
        elif n["status"] == "online":
            close_incident(ctx, "node_down", n["name"], "heartbeat is back")
