"""Watchdog check: cooldown accounts return to idle, and broken accounts stay visible."""
from __future__ import annotations

from ..application_context import ApplicationContext
from ..incidents import close_incident, open_incident
from ..repositories.watchdog import ACCOUNT_MARK_IDLE, COOLDOWN_ACCOUNTS, PROBLEM_ACCOUNTS, WORKING_ACCOUNTS


def account_cooldowns(ctx: ApplicationContext) -> None:
    # accounts
    for a in ctx.db.all(COOLDOWN_ACCOUNTS):
        if not ctx.leases.cooling(a["id"]):
            ctx.db.run(ACCOUNT_MARK_IDLE, (a["id"],))
            close_incident(ctx, "account_quota", a["id"], "cooldown ended")
    for a in ctx.db.all(PROBLEM_ACCOUNTS):
        what = {"cooldown": "is out of quota", "signed_out": "is signed out", "error": "fails its check"}[a["status"]]
        until = f" until {a['cooldown_until']:%Y-%m-%d %H:%M} UTC" if a["cooldown_until"] else ""
        open_incident(ctx, "account_quota", a["id"], "warning", f"account {a['id']} {what}",
                      f"{a['provider']}: {a['last_error'][:300]}{until}. Its tickets go to the other accounts.")
    for a in ctx.db.all(WORKING_ACCOUNTS):
        close_incident(ctx, "account_quota", a["id"], "account works again")
