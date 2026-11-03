"""Install owner approval routes for knowledge policies."""
import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, HTTPException

from ... import knowledge
from ...gateway_helpers import github_control_gateway
from ...platform.events.wiring import publish_for
from .bodies import KnowledgeApprovalBody
from .events import PolicyMerged


log = logging.getLogger("aiflow4.api")


def install_proposal_routes(app, ctx, deps) -> SimpleNamespace:
    owner_auth = deps.owner_auth

    @app.post("/v1/knowledge/proposals/{proposal_id}/approve")
    def knowledge_approve(proposal_id: int, body: KnowledgeApprovalBody,
                          _owner: str = Depends(owner_auth)) -> dict:
        proposal = ctx.db.one("SELECT * FROM managed_resource_proposals WHERE id=%s", (proposal_id,))
        if not proposal:
            raise HTTPException(404, "policy proposal not found")
        if proposal["status"] != "pending":
            raise HTTPException(409, "policy proposal is no longer pending")
        github = github_control_gateway(ctx)
        repo_name = Path(ctx.settings.control_repo).name
        owner = _owner
        try:
            merged = github.merge_pr(repo_name, int(proposal["pr_number"]),
                                     f"Policy: {proposal['title']}", "squash")
        except Exception as e:  # Do not expose provider responses or credentials through the UI.
            log.warning("policy proposal merge failed for PR #%s", proposal["pr_number"])
            raise HTTPException(502, "policy proposal could not be merged") from e
        if not merged.get("merged"):
            return {"merged": False, "message": merged.get("message", "GitHub refused the merge")}
        resource = knowledge.save_current(
            ctx.db, kind="policy", resource_id=proposal["resource_id"], title=proposal["title"],
            content=proposal["content"], archived=proposal["archived"], actor=proposal["actor"],
            action=proposal["action"], path=knowledge.path_for("policy", proposal["resource_id"]),
            commit_sha=str(merged.get("sha") or ""), approved_by=owner)
        with ctx.db.transaction():
            ctx.db.run("UPDATE managed_resource_proposals SET status='merged',merged_at=now(),approved_by=%s WHERE id=%s",
                       (owner, proposal_id))
            publish_for(ctx, PolicyMerged(resource=proposal["resource_id"], actor=owner,
                                          resource_id=proposal["resource_id"], pr_number=proposal["pr_number"],
                                          resource_version=resource["version"], commit=str(merged.get("sha") or "")))
        ctx.events.emit("resource.policy_merged", project=proposal["resource_id"] if proposal["resource_id"] != "default" else "",
                        account=owner, outcome="merged", reason=f"PR #{proposal['pr_number']}",
                        data={"type": "policy", "id": proposal["resource_id"], "version": resource["version"]})
        if ctx.refresh_repo:
            try:
                ctx.refresh_repo()
            except Exception:
                log.warning("control repo refresh after policy merge failed")
        return {"merged": True, "sha": merged.get("sha", ""), "resource": resource}

    return SimpleNamespace(knowledge_approve=knowledge_approve)
