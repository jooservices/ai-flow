"""Route installer groups for fleet.
"""
import uuid
from types import SimpleNamespace
from .bodies import NodeOperationBody
from .events import NodeOperationQueued
from fastapi import HTTPException, Request

def _register_create_node_operation(app, ctx, deps, support=None):
    audit = deps.audit
    console_user = deps.console_user
    operation_plan = support.operation_plan
    @app.post("/v1/nodes/{name}/operations", status_code=202)
    def create_node_operation(name: str, body: NodeOperationBody, request: Request) -> dict:
        from . import routes as fleet_routes
        user = console_user(request, {"nodes.manage"}, target="node", action=body.action, node=name)
        node_row = ctx.db.one("SELECT name, status, token_hash, tools, last_heartbeat"
                              " FROM nodes WHERE name=%s", (name,))
        if not node_row:
            fleet_routes.audit_refused_write(audit, "node", body.action, name, user.subject,
                                              "node is not registered")
            raise HTTPException(404, "node is not registered")
        heartbeat = node_row["last_heartbeat"]
        if (node_row["status"] != "online" or not node_row["token_hash"] or not heartbeat
                or (fleet_routes.utcnow() - heartbeat).total_seconds() > 75):
            fleet_routes.audit_refused_write(audit, "node", body.action, name, user.subject,
                                              "node agent is not online")
            raise HTTPException(409, "node agent must be online to install or repair")
        try:
            payload = operation_plan(name, body, node_row)
        except HTTPException as exc:
            fleet_routes.audit_refused_write(audit, "node", body.action, name, user.subject, str(exc.detail))
            raise
        operation_id = str(uuid.uuid4())
        components = [str(t["name"]) for t in payload["tools"]]
        with ctx.db.transaction() as connection:
            connection.execute("INSERT INTO node_operations (id,node,action,actor,payload) VALUES (%s,%s,%s,%s,%s)",
                               (operation_id, name, body.action, user.subject, fleet_routes.J(payload)))
            fleet_routes.publish_for(ctx, NodeOperationQueued(node=name, actor=user.subject, action=body.action,
                                                 operation_id=operation_id, components=components,
                                                 service_count=len(payload["services"])))
        ctx.events.emit("node.operation.queued", node=name, outcome="queued",
                        reason=f"{body.action}: {', '.join(components) or 'services'}",
                        data={"operation_id": operation_id, "action": body.action, "actor": user.subject})
        return {"id": operation_id, "node": name, "action": body.action, "status": "queued",
                "components": components}
    return create_node_operation

def _register_node_operations(app, ctx, deps, support=None):
    console_user = deps.console_user
    @app.get("/v1/nodes/{name}/operations")
    def node_operations(name: str, request: Request) -> dict:
        console_user(request, {"console.read"})
        rows = ctx.db.all("SELECT id, node, action, actor, status, result, created_at, started_at, completed_at"
                          " FROM node_operations WHERE node=%s AND action <> 'read_logs'"
                          " ORDER BY created_at DESC LIMIT 50", (name,))
        for row in rows:
            row["id"] = str(row["id"])
        return {"node": name, "operations": rows}
    return node_operations

def install_node_operation_admin_routes(app, ctx, deps, helpers):
    support = SimpleNamespace(operation_plan=helpers.operation_plan)
    create = _register_create_node_operation(app, ctx, deps, support)
    listing = _register_node_operations(app, ctx, deps)
    return SimpleNamespace(operation_plan=support.operation_plan, create_node_operation=create,
                           node_operations=listing)
