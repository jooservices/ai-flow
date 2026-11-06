"""Watchdog check: tool waits, environment waits and the daily server-wording scan."""
from __future__ import annotations

from .. import environment as environment_reports
from .. import tooling
from ..application_context import ApplicationContext


def tool_and_environment_waits(ctx: ApplicationContext) -> None:
    # tickets waiting for a tool no machine has: the 24 hour clock and the one maintenance ticket per tool
    tooling.declare_undeclared_services(ctx)
    tooling.sweep(ctx)
    tooling.check_stalled_waits(ctx)
    # environment items a run reported missing, and the daily scan of open tickets for server wording
    environment_reports.sweep(ctx)
    environment_reports.scan_if_due(ctx)
