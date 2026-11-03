"""Routes of the business feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from types import SimpleNamespace

from fastapi import Depends, Header, Query, Request

from ...console_auth import has_permission
from ...state_schema import STATE_RESPONSE_SCHEMA
from . import routes_business, routes_stakeholder, state_view


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import log  # noqa: PLC0415 - api imports this module
    from ...gateway_helpers import github_gateway  # noqa: PLC0415 - api imports this module
    snapshots = state_view.StateSnapshotCache()
    deploy_auth = deps.deploy_auth
    optional_console_user = deps.optional_console_user
    owner_auth = deps.owner_auth
    business_routes = routes_business.install_business_routes(app, ctx)
    stakeholder_routes = routes_stakeholder.install_stakeholder_routes(app, ctx, log)

    @app.get("/v1/state", responses={200: {"description": "Command Center, Delivery, and SA snapshot",
                                            "content": {"application/json": {"schema": STATE_RESPONSE_SCHEMA}}}})
    def state(request: Request, authorization: str = Header(default="")) -> dict:
        if authorization:  # the deploy scripts on the control machine read it with the owner token (login is on)
            owner_auth(request, authorization)
        # The login middleware authenticates console sessions and permits this legacy owner-token route;
        # resolving the cookie here keeps scoped session data filtered while allowing the owner token alone.
        user = optional_console_user(request)
        include_signin = bool(user and has_permission(user.effective_roles, "nodes.manage"))
        data = state_view.cached_snapshot(ctx, snapshots, include_signin)
        if user and user.project_keys is not None:
            data = state_view.restrict_to_projects(ctx, data, set(user.project_keys))
        if user and not has_permission(user.effective_roles, "view.machines_ai"):
            state_view.hide_machines_and_detail(data)
        return data

    @app.get("/v1/deploy/state")
    def deploy_state(_deploy: str = Depends(deploy_auth)) -> dict:
        """The registry state for aiflow4-autodeploy; works with console sign-in on because it checks the token."""
        from ...state import snapshot
        return snapshot(ctx)

    @app.get("/v1/deploy/ci-state")
    def deploy_ci_state(repo: str = Query(pattern=r"^[\w.-]+/[\w.-]+$"), sha: str = Query(pattern=r"^[0-9a-fA-F]{7,64}$"),
                        _deploy: str = Depends(deploy_auth)) -> dict:
        """CI state of a commit (pending|failed|ok|none) through the connection for its repository."""
        from ...canary import ci_state
        gateway = github_gateway(ctx, repo)
        return {"repo": repo, "sha": sha, "state": ci_state(gateway, repo, sha)}

    return SimpleNamespace(business_project=business_routes.business_project,
                           business_change_requests=business_routes.business_change_requests,
                           business_release_readiness=business_routes.business_release_readiness,
                           business_release_progress=business_routes.business_release_progress,
                           business_change_request=business_routes.business_change_request,
                           stakeholder_project=stakeholder_routes.stakeholder_project,
                           stakeholder_changes=stakeholder_routes.stakeholder_changes,
                           stakeholder_view=stakeholder_routes.stakeholder_view,
                           stakeholder_verdict=stakeholder_routes.stakeholder_verdict, state=state,
                           deploy_state=deploy_state, deploy_ci_state=deploy_ci_state)
