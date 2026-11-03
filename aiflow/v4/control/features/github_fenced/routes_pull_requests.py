"""Fenced pull request lookup, creation and merge routes."""
from types import SimpleNamespace

from . import routes as _routes


def install_pull_request_routes(app, ctx, deps, runtime) -> SimpleNamespace:
    @app.get("/v1/github/pr")
    def github_find_pr(run_id: str = _routes.Query(min_length=1), fence: int = _routes.Query(ge=1),
                       repo: str = _routes.Query(min_length=1, max_length=200),
                       head: str = _routes.Query(min_length=1, max_length=240),
                       node: str = _routes.Depends(deps.node_auth)) -> dict:
        run = deps.fenced(run_id, fence, "github", "find_pr", {"repo": repo, "head": head})
        if run["node"] != node:
            raise _routes.HTTPException(403, "run belongs to another node")
        project = ctx.repo().projects.get(run["project"])
        repository = deps.repository_for_run(run, project)
        if not project or not deps.matches_run_repository(repository, repo):
            raise _routes.HTTPException(403, "repository does not belong to the run's project")
        pr = runtime.github_gateway(ctx, repository).find_pr(repository.split("/", 1)[-1], head)
        found = {"found": bool(pr)}
        if pr:
            found.update(url=pr.get("html_url", ""), number=pr.get("number"))
            head_sha = str((pr.get("head") or {}).get("sha") or "")
            if head_sha:
                found["head_sha"] = head_sha
        _routes.publish_for(ctx, _routes.GitHubPrLookedUp(
            run_id=run_id, fence=fence, ticket=run["ticket_key"], actor=run["account_id"], repo=repository,
            head=head, result=found.get("url", "not found")))
        return found

    @app.post("/v1/github/pr")
    def github_pr(body: _routes.PRBody, node: str = _routes.Depends(deps.node_auth)) -> dict:
        run = deps.fenced(body.run_id, body.fence, "github", "pr", {"repo": body.repo, "head": body.head})
        project = ctx.repo().projects.get(run["project"])
        repository = deps.repository_for_run(run, project)
        if not project or not deps.matches_run_repository(repository, body.repo):
            raise _routes.HTTPException(403, "repository does not belong to the run's project")
        github = runtime.github_gateway(ctx, repository)
        settings = deps.settings_for_run(run, project)
        if body.base != settings["base"]:
            raise _routes.HTTPException(403, "pull request base does not match the repository settings")
        pr = github.create_pr(repository.split("/", 1)[-1], body.head, body.base, body.title, body.body, body.draft)
        with ctx.db.transaction():
            ctx.db.run("UPDATE runs SET pr_url=%s, branch=%s WHERE id=%s",
                       (pr["html_url"], body.head, body.run_id))
            _routes.publish_for(ctx, _routes.GitHubPrCreated(
                run_id=body.run_id, fence=body.fence, ticket=run["ticket_key"], actor=run["account_id"],
                repo=repository, url=pr["html_url"], number=pr.get("number"), draft=body.draft))
        ctx.events.emit("github.pr", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="opened", pr_url=pr["html_url"])
        return {"url": pr["html_url"], "number": pr.get("number")}

    return SimpleNamespace(github_find_pr=github_find_pr, github_pr=github_pr)


def install_github_merge_routes(app, ctx, deps, runtime):
    @app.post("/v1/github/merge")
    def github_merge(body: _routes.MergeBody, node: str = _routes.Depends(deps.node_auth)) -> dict:
        run = deps.fenced(body.run_id, body.fence, "github", "merge", {"number": body.number})
        if run["step"] != "verify":
            raise _routes.HTTPException(403, "only a verify run can merge")
        # ready pull requests merge one after another, each brought up to date first (docs/v4/merge-conflicts.md)
        project = ctx.repo().projects.get(run["project"])
        if not project:
            raise _routes.HTTPException(404, "project is not configured")
        repository = deps.repository_for_run(run, project)
        result = _routes.merge_queue.merge_next(
            ctx.db, runtime.github_gateway(ctx, repository), repository, body.number,
            run["ticket_key"], str(run["id"]),
            lambda: deps.merge_command(run, body.number, body.title, body.method, node))
        if result.get("updated") or result.get("conflict"):
            ctx.events.emit("github.merge_queue", ticket=run["ticket_key"], run_id=str(run["id"]),
                            fence=run["fence"], account=run["account_id"], node=node,
                            outcome="updated" if result.get("updated") else "conflict",
                            reason=result.get("message", "")[:200])
        if result.get("merged"):  # the verify step's merge: the commit and the time are kept on the ticket
            _routes.merge_flow.record_merge(
                ctx, run["ticket_key"], _routes.merge_flow.latest_pr(ctx.db, run["ticket_key"]),
                str(result.get("sha") or ""), "flow", settled=False)
        return result

    return github_merge
