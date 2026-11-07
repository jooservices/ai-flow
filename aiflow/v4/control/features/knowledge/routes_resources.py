"""Install routes for versioned knowledge resources."""
import logging
import secrets
from functools import partial
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException, Request

from ... import knowledge
from ...gateway_helpers import github_control_gateway
from ...platform.events.wiring import publish_for
from .bodies import KnowledgeBody, KnowledgeRestoreBody
from .events import PolicyProposed, ResourceCommitted


log = logging.getLogger("aiflow4.api")


def _load_managed_resources(ctx) -> None:
    knowledge.import_local(ctx.db, ctx.repo().root)


def _propose_policy_change(ctx, github, repo_name: str, base: str, resource_id: str, title: str, content: str,
                          archived: bool, actor: str, action: str, current, path: str, payload: str) -> dict:
    branch = f"aiflow/policy-{resource_id.lower()}-{secrets.token_hex(4)}"
    github.create_branch(repo_name, branch, base)
    sha = github.commit_file(repo_name, branch, path, payload, f"policy: propose {action} {resource_id}")
    pr = github.create_control_pr(repo_name, branch, base, f"Policy: {title}",
                                  f"Policy {action} for `{resource_id}` proposed by {actor}.\n\n"
                                  "Merge only after owner approval in the AI Flow Knowledge screen.")
    with ctx.db.transaction():
        ctx.db.run("INSERT INTO managed_resource_proposals (resource_type,resource_id,title,content,archived,"
                   "base_title,base_content,base_archived,base_version,actor,action,branch,pr_number,pr_url)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   ("policy", resource_id, title, content, archived, current["title"] if current else "",
                    current["content"] if current else "", current["archived"] if current else False,
                    current["version"] if current else 0, actor, action, branch, int(pr["number"]),
                    str(pr.get("html_url") or "")))
        publish_for(ctx, PolicyProposed(resource=resource_id, actor=actor, kind="policy",
                                        resource_id=resource_id, branch=branch, pr_number=int(pr["number"]),
                                        pr_url=str(pr["html_url"]), commit=sha))
    ctx.events.emit("resource.policy_proposed", project=resource_id if resource_id != "default" else "",
                    account=actor, outcome="pending", reason=f"PR #{pr['number']}",
                    data={"type": "policy", "id": resource_id})
    return {"pending": True, "type": "policy", "id": resource_id, "title": title,
            "pr_number": int(pr["number"]), "pr_url": str(pr.get("html_url") or ""), "version":
            int(current["version"]) if current else 0}


def _submit_resource_change(ctx, kind: str, resource_id: str, title: str, content: str, archived: bool,
                            actor: str, action: str, expected_version: int | None = None) -> dict:
    try:
        kind, resource_id, title, content, actor = knowledge.validate(kind, resource_id, title, content, actor)
    except knowledge.Invalid as e:
        raise HTTPException(400, str(e)) from e
    github = github_control_gateway(ctx)
    _load_managed_resources(ctx)
    current = knowledge.get_resource(ctx.db, kind, resource_id)
    if expected_version is not None and (not current or int(current["version"]) != expected_version):
        raise HTTPException(409, "resource changed since it was loaded; refresh and try again")
    path = knowledge.path_for(kind, resource_id)
    repo_name = Path(ctx.settings.control_repo).name
    payload = knowledge.file_content(kind, resource_id, title, content, archived)
    try:
        base = github.default_branch(repo_name)
        if kind == "policy":
            return _propose_policy_change(ctx, github, repo_name, base, resource_id, title, content, archived,
                                          actor, action, current, path, payload)

        sha = github.commit_file(repo_name, base, path, payload, f"{kind}: {action} {resource_id}")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - GitHub errors vary between the App and transport implementations
        log.warning("control repo write failed for %s/%s", kind, resource_id)
        raise HTTPException(502, "control repo could not save the resource") from e
    row = knowledge.save_current(ctx.db, kind=kind, resource_id=resource_id, title=title, content=content,
                                 archived=archived, actor=actor, action=action, path=path, commit_sha=sha)
    publish_for(ctx, ResourceCommitted(resource=resource_id, actor=actor, kind=kind, resource_id=resource_id,
                                       resource_version=row["version"], path=path, commit=sha))
    ctx.events.emit("resource.changed", account=actor, outcome=action, reason=f"{kind}/{resource_id}",
                    data={"type": kind, "id": resource_id, "version": row["version"], "commit": sha})
    return row


def install_resource_routes(app, ctx, deps) -> SimpleNamespace:
    console_user = deps.console_user

    @app.get("/v1/knowledge/resources")
    def knowledge_resources(request: Request, include_archived: bool = True) -> dict:
        console_user(request, "knowledge.read")
        _load_managed_resources(ctx)
        return {"resources": knowledge.list_resources(ctx.db, include_archived),
                "proposals": knowledge.proposals(ctx.db)}

    @app.get("/v1/knowledge/resources/{kind}/{resource_id}/history")
    def knowledge_history(kind: str, resource_id: str, request: Request) -> dict:
        console_user(request, "knowledge.read")
        _load_managed_resources(ctx)
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
        return _submit_resource_change(ctx, body.type, body.id, body.title, body.content, body.archived,
                                       user.subject, action, body.version)

    @app.post("/v1/knowledge/resources/{kind}/{resource_id}/restore")
    def knowledge_restore(kind: str, resource_id: str, body: KnowledgeRestoreBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="aiflow-control", action="resource_restore",
                            node=resource_id)
        _load_managed_resources(ctx)
        version = ctx.db.one("SELECT title,content,archived FROM managed_resource_history WHERE resource_type=%s"
                             " AND resource_id=%s AND version=%s", (kind, resource_id, body.version))
        if not version:
            raise HTTPException(404, "resource version not found")
        return _submit_resource_change(ctx, kind, resource_id, version["title"], version["content"],
                                       version["archived"], user.subject, "restore")

    return SimpleNamespace(
        knowledge_resources=knowledge_resources,
        knowledge_history=knowledge_history,
        knowledge_save=knowledge_save,
        knowledge_restore=knowledge_restore,
        load_managed_resources=partial(_load_managed_resources, ctx),
        submit_resource_change=partial(_submit_resource_change, ctx),
    )
