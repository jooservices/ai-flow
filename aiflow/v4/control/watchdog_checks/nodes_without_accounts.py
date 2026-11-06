"""Watchdog check: an online node with no AI account cannot take work."""
from __future__ import annotations

from ..application_context import ApplicationContext
from ..incidents import close_incident, open_incident
from ..repositories.watchdog import NODES_NAME_STATUS


def nodes_without_accounts(ctx: ApplicationContext) -> None:
    repo = ctx.repo()
    # nodes that joined but have no AI account: they add slots, never work
    with_accounts = {m for a in repo.accounts for m in a.machines}
    for n in ctx.db.all(NODES_NAME_STATUS):
        if n["status"] == "online" and n["name"] not in with_accounts:
            where = "" if n["name"] in repo.nodes or repo.inventory_in_db else " and is not described in aiflow-control/nodes/"
            open_incident(ctx, "node_no_ai", n["name"], "warning", f"node {n['name']} has no AI account",
                          f"{n['name']} is online{where}, but no AI account is assigned to it, so it cannot take any ticket."
                          " Add and sign in to an AI account for this machine on the Machines and AI screen.")
        elif n["name"] in with_accounts or n["status"] != "online":
            close_incident(ctx, "node_no_ai", n["name"], "accounts assigned" if n["name"] in with_accounts else "node left")
