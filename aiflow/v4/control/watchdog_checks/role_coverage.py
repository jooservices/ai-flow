"""Watchdog check: every flow role needs two ready accounts of different providers."""
from __future__ import annotations

import logging

from .. import flow_doctor, role_health
from ..application_context import ApplicationContext
from ..incidents import close_incident, open_incident
from ..repositories.watchdog import ONLINE_NODE_NAMES, READY_ACCOUNT_IDS

log = logging.getLogger("aiflow4.loops")


def role_coverage(ctx: ApplicationContext) -> None:
    repo = ctx.repo()
    # every role the flow needs must have at least two ready accounts (no single AI the flow depends on)
    online = {r["name"] for r in ctx.db.all(ONLINE_NODE_NAMES)}
    by_id = {a.id: a for a in repo.accounts}
    ready = {r["id"] for r in ctx.db.all(READY_ACCOUNT_IDS)
             if r["id"] in by_id and online.intersection(by_id[r["id"]].machines)}
    try:  # quality by role: switch roles off and back on, settle probation tickets (role_health.py)
        role_health.evaluate(ctx)
    except Exception as e:  # noqa: BLE001 - role health failure must not stop this check
        log.warning("role health evaluation failed: %s", e)
    switched_off = role_health.disabled_roles(ctx.db)
    for role in flow_doctor.ROLE_COVERAGE_ROLES:
        holders = [a for a in repo.accounts if role in (a.roles or [])]
        ok = [a for a in holders if a.id in ready and not a.paused and (a.id, role) not in switched_off]
        if not holders:
            continue
        providers = {a.provider for a in ok}
        if len(providers) < 2:  # two ready accounts of the same provider do not survive that provider's quota or outage
            open_incident(ctx, "role_coverage", role, "critical" if not ok else "warning",
                          f"role {role} has {len(providers)} ready provider{'s' if len(providers) != 1 else ''}",
                          f"Accounts with the role: {', '.join(a.id for a in holders)}; ready: "
                          f"{', '.join(a.id for a in ok) or 'none'} across "
                          f"{', '.join(sorted(providers)) or 'no providers'}. Every role needs two ready accounts of "
                          "different providers so one quota or outage cannot stop the flow.")
        else:
            close_incident(ctx, "role_coverage", role, f"{len(ok)} accounts from {len(providers)} providers ready")
