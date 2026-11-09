"""The control watchdog runs independent checks in a fixed order (JAIF-308).

Each check is a function in `watchdog_checks`. `CHECKS` lists all fifteen.
`run` calls the two groups below, and each group runs its part of `CHECKS`.
A check that raises is logged and the later checks still run. Add a check by
importing it and inserting it into `CHECKS`.
"""
from __future__ import annotations

import logging

from .application_context import ApplicationContext
from .watchdog_checks.account_cooldowns import account_cooldowns
from .watchdog_checks.lost_leases_and_run_clocks import lost_leases_and_run_clocks
from .watchdog_checks.node_heartbeats import node_heartbeats
from .watchdog_checks.node_services import node_services
from .watchdog_checks.node_tools import node_tools
from .watchdog_checks.nodes_without_accounts import nodes_without_accounts
from .watchdog_checks.orphaned_tickets import orphaned_tickets
from .watchdog_checks.quota_forecasts import quota_forecasts
from .watchdog_checks.quota_windows import quota_windows
from .watchdog_checks.retry_and_deadline_passes import retry_and_deadline_passes
from .watchdog_checks.role_coverage import role_coverage
from .watchdog_checks.stuck_tickets import stuck_tickets
from .watchdog_checks.tool_and_environment_waits import tool_and_environment_waits
from .watchdog_checks.verify_no_verdict import verify_no_verdict
from .watchdog_checks.version_skew import version_skew

log = logging.getLogger("aiflow4.loops")

# The machine and orphan checks come first. The flow and quota checks follow them.
MACHINE_AND_ORPHAN_COUNT = 7
CHECKS = (
    node_heartbeats,
    version_skew,
    node_tools,
    node_services,
    tool_and_environment_waits,
    nodes_without_accounts,
    orphaned_tickets,
    stuck_tickets,
    verify_no_verdict,
    retry_and_deadline_passes,
    lost_leases_and_run_clocks,
    account_cooldowns,
    role_coverage,
    quota_windows,
    quota_forecasts,
)


def run(ctx: ApplicationContext) -> None:
    """Run every watchdog check; a failure in one does not stop the later checks."""
    run_machine_and_orphan_checks(ctx)
    run_flow_and_quota_checks(ctx)


def run_checks(ctx: ApplicationContext, checks: tuple) -> None:
    """Run these checks in order. Log a failure and continue with the next check."""
    for check in checks:
        try:
            check(ctx)
        except Exception:  # noqa: BLE001 - a failing check must not stop the rest of the watchdog
            log.exception("watchdog check %s failed", check.__name__)


def run_machine_and_orphan_checks(ctx: ApplicationContext) -> None:
    """Nodes, the tools and services they owe projects, waits, and orphaned tickets."""
    run_checks(ctx, CHECKS[:MACHINE_AND_ORPHAN_COUNT])


def run_flow_and_quota_checks(ctx: ApplicationContext) -> None:
    """Stuck tickets, holds and deadlines, lost runs, accounts, roles and quota."""
    run_checks(ctx, CHECKS[MACHINE_AND_ORPHAN_COUNT:])
