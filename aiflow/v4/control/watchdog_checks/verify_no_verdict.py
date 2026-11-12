"""Watchdog check: every eligible verifier misses a verdict before one incident opens."""
from __future__ import annotations

from ..application_context import ApplicationContext
from ..verify_no_verdict import check_incidents


def verify_no_verdict(ctx: ApplicationContext) -> None:
    from .. import api as control_api
    check_incidents(ctx, control_api.RELEASES)
