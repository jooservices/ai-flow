"""Fenced GitHub token and Git relay routes."""
from . import routes as _routes


def _github_token_permissions(run, project) -> dict:
    permissions = ({"contents": "read", "metadata": "read"}
                   if run["step"] in ("triage", "version_release", "post_merge") else
                   {"contents": "write", "metadata": "read", "workflows": "write"})
    if run["step"] == "version_release" and not _routes.version_release.enabled(project):
        raise _routes.HTTPException(403, "project is not enabled for version releases")
    return permissions


def _validate_git_transfer_branch(run, branch: str) -> None:
    if branch and (run["step"] != "dev" or not branch.startswith(f"{run['ticket_key']}-")
                   or not _routes.re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", branch)
                   or branch.startswith("/") or branch.endswith("/") or ".." in branch
                   or "@{" in branch):
        raise _routes.HTTPException(422, "the Git transfer branch is invalid for this run")


def install_github_token_routes(app, ctx, deps, runtime):
    @app.post("/v1/github/token")
    def github_token(body: _routes.GitHubTokenBody, node: str = _routes.Depends(deps.node_auth)) -> dict:
        run = deps.fenced(body.run_id, body.fence, "github", "token", {"repo": body.repo})
        project = ctx.repo().projects.get(run["project"])
        repository = deps.repository_for_run(run, project)
        if not project or not deps.matches_run_repository(repository, body.repo):
            raise _routes.HTTPException(403, "repository does not belong to the run's project")
        permissions = _github_token_permissions(run, project)
        github = runtime.github_gateway(ctx, repository)
        if getattr(github, "kind", "app") == "token":
            branch = body.branch.strip()
            _validate_git_transfer_branch(run, branch)
            expires = _routes.utcnow() + _routes.timedelta(hours=1)
            connection_id = _routes.github_connection_used.get()
            capability = runtime.issue_git_proxy_token(ctx, {
                "connection": connection_id, "repo": repository.lower(), "run_id": body.run_id,
                "fence": body.fence, "node": node, "branch": branch,
                "expires": int(expires.timestamp()),
            })
            _routes.publish_for(ctx, _routes.GitHubGitRelayTokenIssued(
                run_id=body.run_id, fence=body.fence, ticket=run["ticket_key"], actor=run["account_id"],
                repo=repository, branch=branch, expires=expires.isoformat()))
            return {"token": capability, "expires_at": expires.isoformat(),
                    "transport_url": f"{ctx.settings.control_url.rstrip('/')}/v1/github/git/{repository}.git"}
        token, expires = github.token(repository.split("/", 1)[-1], permissions)
        _routes.publish_for(ctx, _routes.GitHubTokenIssued(
            run_id=body.run_id, fence=body.fence, ticket=run["ticket_key"], actor=run["account_id"],
            repo=repository, expires=expires.isoformat()))
        return {"token": token, "expires_at": expires.isoformat()}

    return github_token


def install_git_proxy_routes(app, ctx, deps, runtime):
    async def github_git_proxy(git_path: str, request: _routes.Request) -> _routes.Response:
        """Broker token-backed Git smart HTTP without exposing the stored access token to a worker."""
        capability = runtime.verify_git_proxy_token(ctx, runtime.git_proxy_credential(request))
        if not capability:
            raise _routes.HTTPException(401, "Git repository transfer credential is invalid or expired",
                                        headers={"WWW-Authenticate": 'Basic realm="AI Flow Git transfer"'})
        repository, suffix, operation = _routes._git_transfer_request(git_path, request, capability)
        run, run_id, fence = _routes._authorized_git_transfer(
            capability, repository, operation, ctx=ctx, fleet_routes=deps.fleet_routes, run_row=deps.run_row,
            matches_run_repository=deps.matches_run_repository, repository_for_run=deps.repository_for_run)
        branch = str(capability.get("branch") or "")
        upstream = await _github_git_proxy_upstream(
            ctx, capability, repository, suffix, operation, branch, request, runtime)
        if request.method == "POST" and operation == "git-receive-pack":
            if run is None:
                raise _routes.HTTPException(403, "check-probe Git credentials are read-only")
            accepted = 200 <= upstream.status_code < 300
            if accepted:
                _routes.publish_for(ctx, _routes.GitHubPushAccepted(
                    run_id=run_id, fence=fence, ticket=run["ticket_key"], actor=run["account_id"],
                    repo=repository, branch=branch))
            else:
                deps.audit("github", "git-push", run["ticket_key"], run_id, fence, run["account_id"], False,
                           f"GitHub returned {upstream.status_code}", {"repo": repository, "branch": branch})
        response_headers = {key: value for key, value in upstream.headers.items()
                            if key.lower() in {"content-type", "cache-control", "expires", "pragma"}}
        return _routes.Response(content=upstream.content, status_code=upstream.status_code,
                                headers=response_headers)

    @app.get("/v1/github/git/{git_path:path}", operation_id="githubGitProxyGet")
    async def github_git_proxy_get(git_path: str, request: _routes.Request) -> _routes.Response:
        return await github_git_proxy(git_path, request)

    @app.post("/v1/github/git/{git_path:path}", operation_id="githubGitProxyPost")
    async def github_git_proxy_post(git_path: str, request: _routes.Request) -> _routes.Response:
        return await github_git_proxy(git_path, request)


async def _github_git_proxy_upstream(ctx, capability, repository, suffix, operation, branch, request, runtime):
    body_bytes = await request.body() if request.method == "POST" else b""
    if request.method == "POST" and operation == "git-receive-pack":
        targets = runtime.receive_pack_target(body_bytes)
        if targets != {f"refs/heads/{branch}"}:
            raise _routes.HTTPException(403, "Git push is limited to the branch assigned to this run")
    connection_id = str(capability.get("connection") or "")
    gateway = runtime.github_gateway_for_connection(ctx, connection_id, repository)
    if getattr(gateway, "kind", "app") != "token":
        raise _routes.HTTPException(403, "Git relay credentials are only valid for access-token connections")
    upstream_token, _ = gateway.token(repository.split("/", 1)[-1])
    forwarded_headers = {key: value for key, value in request.headers.items()
                         if key.lower() in {"accept", "content-type", "git-protocol"}}
    try:
        import anyio
        return await anyio.to_thread.run_sync(
            _routes._github_git_upstream, repository, suffix, request.method,
            list(request.query_params.multi_items()), forwarded_headers, body_bytes, upstream_token)
    except _routes.httpx.HTTPError as exc:
        runtime.log.warning("GitHub repository transfer failed (%s)", type(exc).__name__)
        raise _routes.HTTPException(502, "GitHub repository transfer failed") from exc
