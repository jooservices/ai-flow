"""Reactions of the work feature: the run timeline rows (`run.claimed`, `run.finished`) are listeners of the run events."""
from __future__ import annotations

from ...platform.events import IN_TRANSACTION, Registry
from ...platform.events.wiring import publish_for
from .events import RunFinished, RunStarted


def register_listeners(ctx, registry: Registry) -> None:
    @registry.on(RunStarted, delivery=IN_TRANSACTION, name="work.telemetry_run_started")
    def telemetry_run_started(envelope, item: RunStarted) -> None:
        ctx.events.emit("run.claimed", ticket=item.ticket_key, project=item.project, step=item.step, run_id=item.run_id,
                        fence=item.fence, account=item.account, provider=item.provider, model=item.model, node=item.node)

    @registry.on(RunFinished, delivery=IN_TRANSACTION, name="work.telemetry_run_finished")
    def telemetry_run_finished(envelope, item: RunFinished) -> None:
        extra = {k: v for k, v in (("tokens_in", item.usage_in), ("tokens_out", item.usage_out),
                                   ("cost_usd", item.cost_usd), ("still_owner", item.still_owner),
                                   ("pr_url", item.pr_url or None)) if v is not None}
        ctx.events.emit("run.finished", ticket=item.ticket_key, project=item.project, step=item.step,
                        run_id=item.run_id, fence=item.fence, account=item.account, provider=item.provider,
                        model=item.model or None, node=item.node, duration_ms=item.duration_ms, outcome=item.outcome,
                        reason=item.reason, **extra)


def run_finished(ctx, **fields) -> None:
    """Record that a run ended: the event and its timeline row are written together."""
    with ctx.db.transaction():
        publish_for(ctx, RunFinished(**fields))
