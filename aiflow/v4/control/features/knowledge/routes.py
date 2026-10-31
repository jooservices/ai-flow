"""Routes of the knowledge feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import secrets
from pathlib import Path
from types import SimpleNamespace

from .bodies import KnowledgeApprovalBody, KnowledgeBody, KnowledgeItemActionBody, KnowledgeItemBody, KnowledgeRestoreBody
from fastapi import Depends, HTTPException, Request
from ... import (knowledge)


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import log  # noqa: PLC0415 - api imports this module
    from ...gateway_helpers import github_control_gateway  # noqa: PLC0415 - api imports this module
    audit = deps.audit
    console_user = deps.console_user
    owner_auth = deps.owner_auth
    # --- memory: past tickets, run summaries and verify findings ------------------------------------------------


    # --- learned knowledge: automatic capture, isolated context packs, and Knowledge screen data --------------
    @app.get("/v1/knowledge/items")
    def knowledge_items(request: Request, project: str = "", q: str = "", include_retired: bool = True) -> dict:
        console_user(request, "knowledge.read")
        return knowledge.dashboard(ctx.db, project, q, include_retired)

    @app.post("/v1/knowledge/items")
    def knowledge_item_save(body: KnowledgeItemBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="knowledge", action="item_save")
        try:
            item = knowledge.save_item(ctx.db, project=body.project, scope=body.scope, epic=body.epic, kind=body.kind,
                                       title=body.title, content=body.content, actor=user.subject)
        except knowledge.Invalid as exc:
            raise HTTPException(422, str(exc)) from exc
        audit("knowledge", "item_save", str(item["id"]), "", None, user.subject, True, "saved",
              {"kind": body.kind, "project": body.project, "scope": body.scope})
        return knowledge._pack_item(item)

    @app.post("/v1/knowledge/items/{item_id}/{action}")
    def knowledge_item_action(item_id: int, action: str, body: KnowledgeItemActionBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="knowledge", action="item_" + action,
                            node=str(item_id))
        try:
            item = knowledge.moderate_item(ctx.db, item_id, action=action, actor=user.subject,
                                           content=body.content, title=body.title,
                                           duplicate_id=body.duplicate_id, project=body.project,
                                           scope=body.scope or None, epic=body.epic)
        except knowledge.Invalid as exc:
            raise HTTPException(422 if "not found" not in str(exc) else 404, str(exc)) from exc
        audit("knowledge", "item_" + action, str(item_id), "", None, user.subject, True, "updated",
              {"item_id": item_id})
        return knowledge._pack_item(item)

    # --- Knowledge screen: versioned control-repo resources -------------------------------------------------
    def load_managed_resources() -> None:
        knowledge.import_local(ctx.db, ctx.repo().root)

    def submit_resource_change(kind: str, resource_id: str, title: str, content: str, archived: bool,
                               actor: str, action: str, expected_version: int | None = None) -> dict:
        try:
            kind, resource_id, title, content, actor = knowledge.validate(kind, resource_id, title, content, actor)
        except knowledge.Invalid as e:
            raise HTTPException(400, str(e)) from e
        github = github_control_gateway(ctx)
        load_managed_resources()
        current = knowledge.get_resource(ctx.db, kind, resource_id)
        if expected_version is not None and (not current or int(current["version"]) != expected_version):
            raise HTTPException(409, "resource changed since it was loaded; refresh and try again")
        path = knowledge.path_for(kind, resource_id)
        repo_name = Path(ctx.settings.control_repo).name
        payload = knowledge.file_content(kind, resource_id, title, content, archived)
        try:
            base = github.default_branch(repo_name)
            if kind == "policy":
                branch = f"aiflow/policy-{resource_id.lower()}-{secrets.token_hex(4)}"
                github.create_branch(repo_name, branch, base)
                sha = github.commit_file(repo_name, branch, path, payload,
                                             f"policy: propose {action} {resource_id}")
                pr = github.create_control_pr(repo_name, branch, base, f"Policy: {title}",
                                                  f"Policy {action} for `{resource_id}` proposed by {actor}.\n\n"
                                                  "Merge only after owner approval in the AI Flow Knowledge screen.")
                ctx.db.run("INSERT INTO managed_resource_proposals (resource_type,resource_id,title,content,archived,"
                           "base_title,base_content,base_archived,base_version,actor,action,branch,pr_number,pr_url)"
                           " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (kind, resource_id, title, content, archived, current["title"] if current else "",
                            current["content"] if current else "", current["archived"] if current else False,
                            current["version"] if current else 0, actor, action, branch, int(pr["number"]),
                            str(pr.get("html_url") or "")))
                audit("aiflow-control", "policy-pr", "", "", None, actor, True, str(pr["html_url"]),
                      {"type": kind, "id": resource_id, "branch": branch, "pr": int(pr["number"]), "commit": sha})
                ctx.events.emit("resource.policy_proposed", project=resource_id if resource_id != "default" else "",
                                account=actor, outcome="pending", reason=f"PR #{pr['number']}",
                                data={"type": kind, "id": resource_id})
                return {"pending": True, "type": kind, "id": resource_id, "title": title,
                        "pr_number": int(pr["number"]), "pr_url": str(pr.get("html_url") or ""), "version":
                        int(current["version"]) if current else 0}

            sha = github.commit_file(repo_name, base, path, payload, f"{kind}: {action} {resource_id}")
        except HTTPException:
            raise
        except Exception as e:  # GitHub errors vary between App and transport implementations.
            log.warning("control repo write failed for %s/%s", kind, resource_id)
            raise HTTPException(502, "control repo could not save the resource") from e
        row = knowledge.save_current(ctx.db, kind=kind, resource_id=resource_id, title=title, content=content,
                                     archived=archived, actor=actor, action=action, path=path, commit_sha=sha)
        audit("aiflow-control", "resource-commit", "", "", None, actor, True, sha,
              {"type": kind, "id": resource_id, "version": row["version"], "path": path})
        ctx.events.emit("resource.changed", account=actor, outcome=action, reason=f"{kind}/{resource_id}",
                        data={"type": kind, "id": resource_id, "version": row["version"], "commit": sha})
        return row

    @app.get("/v1/knowledge/resources")
    def knowledge_resources(request: Request, include_archived: bool = True) -> dict:
        console_user(request, "knowledge.read")
        load_managed_resources()
        return {"resources": knowledge.list_resources(ctx.db, include_archived),
                "proposals": knowledge.proposals(ctx.db)}

    @app.get("/v1/knowledge/resources/{kind}/{resource_id}/history")
    def knowledge_history(kind: str, resource_id: str, request: Request) -> dict:
        console_user(request, "knowledge.read")
        load_managed_resources()
        if kind not in knowledge.KINDS:
            raise HTTPException(404, "unknown resource type")
        return {"type": kind, "id": resource_id, "history": knowledge.history(ctx.db, kind, resource_id),
                "proposals": knowledge.proposals(ctx.db, kind, resource_id)}

    @app.post("/v1/knowledge/resources")
    def knowledge_save(body: KnowledgeBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="aiflow-control", action="resource_save")
        if not body.id.strip():
            raise HTTPException(400, "resource id is required")
        action = "archive" if body.archived else "save"
        return submit_resource_change(body.type, body.id, body.title, body.content, body.archived, user.subject, action,
                                      body.version)

    @app.post("/v1/knowledge/resources/{kind}/{resource_id}/restore")
    def knowledge_restore(kind: str, resource_id: str, body: KnowledgeRestoreBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="aiflow-control", action="resource_restore",
                            node=resource_id)
        load_managed_resources()
        version = ctx.db.one("SELECT title,content,archived FROM managed_resource_history WHERE resource_type=%s"
                             " AND resource_id=%s AND version=%s", (kind, resource_id, body.version))
        if not version:
            raise HTTPException(404, "resource version not found")
        return submit_resource_change(kind, resource_id, version["title"], version["content"], version["archived"],
                                      user.subject, "restore")

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
        ctx.db.run("UPDATE managed_resource_proposals SET status='merged',merged_at=now(),approved_by=%s WHERE id=%s",
                   (owner, proposal_id))
        audit("aiflow-control", "policy-merge", "", "", None, owner, True, str(merged.get("sha") or ""),
              {"id": proposal["resource_id"], "pr": proposal["pr_number"], "version": resource["version"]})
        ctx.events.emit("resource.policy_merged", project=proposal["resource_id"] if proposal["resource_id"] != "default" else "",
                        account=owner, outcome="merged", reason=f"PR #{proposal['pr_number']}",
                        data={"type": "policy", "id": proposal["resource_id"], "version": resource["version"]})
        if ctx.refresh_repo:
            try:
                ctx.refresh_repo()
            except Exception:
                log.warning("control repo refresh after policy merge failed")
        return {"merged": True, "sha": merged.get("sha", ""), "resource": resource}

    return SimpleNamespace(knowledge_items=knowledge_items, knowledge_item_save=knowledge_item_save, knowledge_item_action=knowledge_item_action, load_managed_resources=load_managed_resources, submit_resource_change=submit_resource_change, knowledge_resources=knowledge_resources, knowledge_history=knowledge_history, knowledge_save=knowledge_save, knowledge_restore=knowledge_restore, knowledge_approve=knowledge_approve)
