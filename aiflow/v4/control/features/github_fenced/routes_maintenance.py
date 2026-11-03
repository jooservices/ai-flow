"""Fenced GitHub branch maintenance, check and comment routes."""
from types import SimpleNamespace

from . import routes as _routes


def install_github_maintenance_routes(app, ctx, deps, runtime) -> SimpleNamespace:
    @app.post("/v1/github/update-branch")
    def github_update_branch(body: _routes.PRCommentBody, node: str = _routes.Depends(deps.node_auth)) -> dict:
        run = deps.fenced(body.run_id, body.fence, "github", "update-branch", {"number": body.number})
        if run["step"] != "verify":
            raise _routes.HTTPException(403, "only a verify run updates a PR branch")
        project = ctx.repo().projects[run["project"]]
        repository = deps.repository_for_run(run, project)
        github = runtime.github_gateway(ctx, repository)
        result = github.update_branch(repository.split("/", 1)[-1], body.number)
        if result["ok"]:
            _routes.publish_for(ctx, _routes.GitHubBranchUpdated(
                run_id=body.run_id, fence=body.fence, ticket=run["ticket_key"], actor=run["account_id"],
                repo=repository, number=body.number, message=result["message"][:200]))
        else:
            deps.audit("github", "update-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"],
                       False, result["message"][:200], {"number": body.number, "repo": repository})
        return result

    @app.get("/v1/github/checks")
    def github_checks(run_id: str, fence: int, sha: str, node: str = _routes.Depends(deps.node_auth)) -> dict:
        run = deps.run_row(run_id)
        if run["node"] != node or not ctx.leases.valid(run["ticket_key"], run_id, fence):
            raise _routes.HTTPException(409, "not the owner of this run")
        project = ctx.repo().projects[run["project"]]
        repository = deps.repository_for_run(run, project)
        github = runtime.github_gateway(ctx, repository)
        return {"checks": github.checks(repository.split("/", 1)[-1], sha)}

    @app.post("/v1/github/comment")
    def github_comment(body: _routes.PRCommentBody, node: str = _routes.Depends(deps.node_auth)) -> dict:
        run = deps.fenced(body.run_id, body.fence, "github", "comment", {"number": body.number})
        project = ctx.repo().projects[run["project"]]
        repository = deps.repository_for_run(run, project)
        github = runtime.github_gateway(ctx, repository)
        outcome = _routes.pr_comments.post(
            github, repository.split("/", 1)[-1], body.number, body.kind, body.body)
        _routes.publish_for(ctx, _routes.GitHubCommentPosted(
            run_id=body.run_id, fence=body.fence, ticket=run["ticket_key"], actor=run["account_id"],
            repo=repository, number=body.number, kind=body.kind, outcome=outcome))
        return {"ok": True, "outcome": outcome}

    @app.post("/v1/github/delete-branch")
    def github_delete_branch(body: _routes.BranchCleanupBody,
                             node: str = _routes.Depends(deps.node_auth)) -> dict:
        """Delete the ticket's remote work branch through the connection assigned to its repository."""
        run = deps.fenced(body.run_id, body.fence, "github", "delete-branch", {"branch": body.branch})
        if run["step"] not in ("dev", "verify"):
            raise _routes.HTTPException(403, "only a development or verify run cleans up a branch")
        branch = body.branch.strip()
        if (not branch.startswith(f"{run['ticket_key']}-")
                or not _routes.re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", branch)
                or branch.endswith("/") or ".." in branch):
            raise _routes.HTTPException(422, "the branch does not belong to this ticket")
        project = ctx.repo().projects[run["project"]]
        repository = deps.repository_for_run(run, project)
        github = runtime.github_gateway(ctx, repository)
        try:
            deleted = github.delete_branch(repository.split("/", 1)[-1], branch)
        except _routes.GitHubError as exc:
            deps.audit("github", "delete-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"],
                       False, "GitHub refused the branch deletion", {"branch": branch, "repo": repository})
            raise _routes.HTTPException(502, "GitHub refused the branch deletion") from exc
        _routes.publish_for(ctx, _routes.GitHubBranchDeleted(
            run_id=body.run_id, fence=body.fence, ticket=run["ticket_key"], actor=run["account_id"],
            repo=repository, branch=branch, deleted=bool(deleted)))
        return {"ok": True, "deleted": deleted}

    return SimpleNamespace(github_update_branch=github_update_branch, github_checks=github_checks,
                           github_comment=github_comment, github_delete_branch=github_delete_branch)
