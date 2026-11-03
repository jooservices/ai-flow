"""Route installer groups for fleet.
"""
import re
from types import SimpleNamespace
from .bodies import JoinCommandBody
from .events import NodeJoinCommandCreated
from fastapi import HTTPException, Request


def _validate_join_command_name(name, body, user, audit) -> None:
    if name != body.name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
        from . import routes as fleet_routes
        fleet_routes.audit_refused_write(audit, "node", "join_command", name, user.subject, "invalid node name")
        raise HTTPException(400, "invalid node name")


def _register_create_join_command(app, ctx, deps, support=None):
    join_bootstrap_command = support.join_bootstrap_command
    validated_join_control_url = support.validated_join_control_url
    audit = deps.audit
    console_user = deps.console_user
    create_join_grant = deps.create_join_grant
    current_release = deps.current_release
    release_assets = deps.release_assets
    @app.post("/v1/nodes/{name}/join-command")
    def create_join_command(name: str, body: JoinCommandBody, request: Request) -> dict:
        from . import routes as fleet_routes
        user = console_user(request, {"nodes.manage"}, target="node", action="join_command", node=name)
        _validate_join_command_name(name, body, user, audit)
        configured = ctx.repo().nodes.get(name)
        role = body.role or (configured.role if configured else "worker")
        project = body.project or (configured.projects[0] if configured and len(configured.projects) == 1 else "")
        if not project:
            fleet_routes.audit_refused_write(audit, "node", "join_command", name, user.subject, "project is required")
            raise HTTPException(422, "select a project for this node")
        try:
            control = validated_join_control_url(ctx.settings.join_control_url or ctx.settings.public_url)
        except ValueError as exc:
            fleet_routes.audit_refused_write(audit, "node", "join_command", name, user.subject, str(exc))
            raise HTTPException(503, "configure AIFLOW4_JOIN_CONTROL_URL to an address reachable from joining nodes") from exc
        version = current_release()
        try:
            release_assets(version)
        except HTTPException as exc:
            fleet_routes.audit_refused_write(audit, "node", "join_command", name, user.subject,
                                              "no signed release is published")
            raise HTTPException(409, "no signed release is published; run aiflow4-deploy first") from exc
        try:
            token, expires_at = create_join_grant(name, role, project)
        except HTTPException as exc:
            fleet_routes.audit_refused_write(audit, "node", "join_command", name, user.subject, str(exc.detail))
            raise
        fleet_routes.publish_for(ctx, NodeJoinCommandCreated(node=name, actor=user.subject, expires_at=expires_at.isoformat(),
                                                role=role, project=project))
        command = join_bootstrap_command(token, control)
        ctx.events.emit("node.join_command.created", node=name, outcome="created",
                        reason="one-time token expires in five minutes", data={"actor": user.subject})
        return {"node": name, "role": role, "project": project, "command": command,
                "expires_at": expires_at.isoformat(), "expires_in_s": 300}
    return create_join_command

def install_join_command_routes(app, ctx, deps):
    from ...gateway_helpers import join_bootstrap_command, validated_join_control_url
    support = SimpleNamespace(join_bootstrap_command=join_bootstrap_command,
                              validated_join_control_url=validated_join_control_url)
    handler = _register_create_join_command(app, ctx, deps, support)
    return SimpleNamespace(create_join_command=handler)
