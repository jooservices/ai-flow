"""Orchestrate the ordered, atomic stages of `POST /v1/work/claim`."""
from __future__ import annotations

from typing import Any

from .repository import WorkRepository
from . import claim_extras, claim_steps


def claim(ctx, deps, body, node: str) -> Any:
    """Run the claim gates, acquisitions, registration and response builders in their fixed order."""
    work = claim_steps.Claim(ctx=ctx, deps=deps, body=body, node=node, store=WorkRepository(ctx.db))
    # Each gate is called by name: the documentation check follows these calls statically to the routes they affect.
    refusal = (claim_steps.refuse_early(work) or claim_steps.select_ticket(work)
               or claim_steps.acquire_probation(work) or claim_steps.acquire_lease(work)
               or claim_steps.reserve_hot_files(work))
    if refusal:
        return refusal
    claim_steps.repository_settings(work)
    claim_steps.dev_assignment(work)
    refusal = claim_steps.register_run(work)
    if refusal:
        return refusal
    claim_steps.record_claim(work)

    extra: dict[str, Any] = {}
    if body.step == "dev":
        extra.update(claim_extras.dev_extras(work))
    if body.step == "verify":
        extra.update(claim_extras.verify_extras(work))
    if body.step == "triage":
        extra.update(claim_extras.triage_extras(work))
    if body.step == "post_merge":
        extra.update(claim_extras.post_merge_extras(work))
    if body.step == "intake":
        extra.update(claim_extras.intake_extras(work))
    if body.step == "impact":
        extra.update(claim_extras.impact_extras(work))
    if body.step == "master":
        extra.update(claim_extras.master_extras(work))
    else:
        extra.update(claim_extras.related_extras(work))
    claim_extras.context_pack(work, extra)
    return claim_extras.final_response(work, extra)
