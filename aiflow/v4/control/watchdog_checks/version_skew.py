"""Watchdog check: a node outside the release policy gets no new work."""
from __future__ import annotations

from .. import rollout
from ..application_context import ApplicationContext


def version_skew(ctx: ApplicationContext) -> None:
    # version skew: a node outside the control/node policy gets no new work until it runs a compatible release
    from .. import api as control_api
    rollout.watch_skew(ctx, control_api.RELEASES)
