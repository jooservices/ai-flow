"""Watchdog check: retry holds, escalation deadlines, idle flow and stack pins."""
from __future__ import annotations

import logging

from ..application_context import ApplicationContext
from ..incidents import check_idle_flow, check_stack_pins

log = logging.getLogger("aiflow4.loops")


def retry_and_deadline_passes(ctx: ApplicationContext) -> None:
    if ctx.jira:  # the third unsuccessful run in a row ends the automatic retries of that ticket
        from .. import retry_hold
        try:
            retry_hold.apply(ctx)
        except Exception as e:  # noqa: BLE001 - a failing hold pass must not stop the rest of the watchdog
            log.warning("retry hold pass failed: %s", e)

    try:  # an unanswered owner question with a reversible recommendation is applied after its deadline
        from .. import escalation_deadline
        escalation_deadline.apply_due(ctx)
    except Exception as e:  # noqa: BLE001 - a failing deadline pass must not stop the rest of the watchdog
        log.warning("escalation deadline pass failed: %s", e)
    # a flow that stands still and a configuration that keeps a project from getting work
    try:
        check_stack_pins(ctx)
        check_idle_flow(ctx)
    except Exception as e:  # noqa: BLE001 - a failing check must not stop the rest of the watchdog
        log.warning("idle and pin checks failed: %s", e)
