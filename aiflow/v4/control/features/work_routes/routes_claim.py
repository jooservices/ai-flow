"""Installer for the work claim route group."""

from typing import Any

from fastapi import Depends

from .bodies import ClaimBody


def install_claim_routes(app, ctx, deps, work_deps, master_candidate, capacity_history, related_hits, bot_identity):
    """Register the work claim handler and return it for the installer result."""

    @app.post("/v1/work/claim")
    def claim(body: ClaimBody, node: str = Depends(deps.node_auth)) -> Any:
        from .routes import _call_claim_work  # noqa: PLC0415 - preserve call-time patching and docs call tracing

        work_deps.master_candidate, work_deps.capacity_history = master_candidate, capacity_history
        work_deps.related_hits, work_deps.bot_identity = related_hits, bot_identity
        return _call_claim_work(ctx, work_deps, body, node)

    return claim
