"""Route installer groups for fleet.
"""
from datetime import timedelta
from types import SimpleNamespace
from .bodies import CheckProbeBody, CheckProbeTokenBody
from .events import CheckProbeTokenIssued
from fastapi import Depends, HTTPException
from ... import tooling

def _register_check_probe_token(app, ctx, deps, support=None):
    node_auth = deps.node_auth
    probe_project = support.probe_project
    _issue_git_proxy_token = support.issue_git_proxy_token
    github_gateway = support.github_gateway
    @app.post("/v1/nodes/{name}/check-probes/token")
    def check_probe_token(name: str, body: CheckProbeTokenBody, node: str = Depends(node_auth)) -> dict:
        """Read-only token for the clean checkout on which a node runs a project's check commands."""
        from . import routes as fleet_routes
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        project = probe_project(name, body.project)
        if body.repo not in project.configured_repositories:
            raise HTTPException(403, "repository does not belong to the project")
        github = github_gateway(ctx, body.repo)
        connection_id = fleet_routes.github_connection_used.get()
        if getattr(github, "kind", "app") == "token":
            expires = fleet_routes.utcnow() + timedelta(hours=1)
            token = _issue_git_proxy_token(ctx, {
                "purpose": "check_probe", "connection": connection_id, "repo": body.repo.lower(),
                "node": node, "project": body.project, "expires": int(expires.timestamp()),
            })
            fleet_routes.publish_for(ctx, CheckProbeTokenIssued(node=node, repo=body.repo, project=body.project,
                                                   connection=connection_id, expires=expires.isoformat(),
                                                   transport="control"))
            return {"token": token, "expires_at": expires.isoformat(),
                    "transport_url": f"{ctx.settings.control_url.rstrip('/')}/v1/github/git/{body.repo}.git"}
        token, expires = github.token(body.repo.split("/", 1)[-1], {"contents": "read", "metadata": "read"})
        fleet_routes.publish_for(ctx, CheckProbeTokenIssued(node=node, repo=body.repo, project=body.project,
                                               connection=connection_id, expires=expires.isoformat(), transport=""))
        return {"token": token, "expires_at": expires.isoformat()}
    return check_probe_token

def _register_save_check_probe(app, ctx, deps, support=None):
    node_auth = deps.node_auth
    probe_project = support.probe_project
    @app.post("/v1/nodes/{name}/check-probes")
    def save_check_probe(name: str, body: CheckProbeBody, node: str = Depends(node_auth)) -> dict:
        """A node reports which check commands of a project ran on a clean checkout and which could not."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        project = probe_project(name, body.project)
        if tooling.probe_signature(tooling.probe_items(project)) != body.signature:
            raise HTTPException(409, "the project's check commands changed; run the probe again")
        results = [{k: str(v)[:300] for k, v in item.items() if k in ("repo", "command", "status", "tool", "detail")}
                   for item in body.results]
        tooling.save_probe(ctx, name, body.project, body.signature, results)
        blocked = [r for r in results if r.get("status") == "cannot_run"]
        ctx.events.emit("project.checks_probed", project=body.project, node=node,
                        outcome="cannot_run" if blocked else "ok",
                        reason=("cannot run: " + "; ".join(f"{r.get('command', '')} ({r.get('tool', '')})" for r in blocked))[:300]
                        if blocked else "")
        return {"saved": True, "cannot_run": len(blocked)}
    return save_check_probe

def install_check_probe_routes(app, ctx, deps, helpers):
    from ...gateway_helpers import _issue_git_proxy_token, github_gateway
    support = SimpleNamespace(probe_project=helpers.probe_project,
                             issue_git_proxy_token=_issue_git_proxy_token, github_gateway=github_gateway)
    token = _register_check_probe_token(app, ctx, deps, support)
    save = _register_save_check_probe(app, ctx, deps, support)
    return SimpleNamespace(probe_project=support.probe_project, check_probe_token=token, save_check_probe=save)
