"""Route installer groups for fleet.
"""
from types import SimpleNamespace
from .bodies import ConfirmBody
from .events import NodeRemoved, NodeWorkAcceptanceChanged
from fastapi import HTTPException, Request

def _register_set_node_work_acceptance(app, ctx, deps, support=None):
    audit = deps.audit
    console_user = deps.console_user
    @app.put("/v1/nodes/{name}/work-acceptance")
    def set_node_work_acceptance(name: str, body: ConfirmBody, accepting_work: bool, request: Request) -> dict:
        from . import routes as fleet_routes
        action = "resume_work" if accepting_work else "stop_taking_work"
        user = console_user(request, {"nodes.manage"}, target="node", action=action, node=name)
        if not accepting_work and not body.confirm:
            fleet_routes.audit_refused_write(audit, "node", action, name, user.subject, "confirmation required")
            raise HTTPException(400, "confirm stopping new work on this machine")
        row = ctx.db.one("SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL", (name,))
        if not row:
            raise HTTPException(404, "machine is not registered")
        with ctx.db.transaction():
            ctx.db.run("UPDATE nodes SET accepting_work=%s,status=CASE WHEN last_heartbeat > now()-interval '75 seconds'"
                       " THEN CASE WHEN %s THEN 'online' ELSE 'draining' END ELSE status END WHERE name=%s",
                       (accepting_work, accepting_work, name))
            fleet_routes.publish_for(ctx, NodeWorkAcceptanceChanged(node=name, actor=user.subject,
                                                       accepting_work=accepting_work))
        ctx.events.emit("node.work_acceptance_changed", node=name, outcome=action,
                        reason="accepting new work" if accepting_work else "new work disabled")
        return {"node": name, "accepting_work": accepting_work}
    return set_node_work_acceptance

def _register_remove_managed_node(app, ctx, deps, support=None):
    audit = deps.audit
    console_user = deps.console_user
    @app.delete("/v1/nodes/{name}")
    def remove_managed_node(name: str, body: ConfirmBody, request: Request) -> dict:
        from . import routes as fleet_routes
        user = console_user(request, {"nodes.manage"}, target="node", action="remove", node=name)
        if not body.confirm:
            fleet_routes.audit_refused_write(audit, "node", "remove", name, user.subject, "confirmation required")
            raise HTTPException(400, "confirm machine removal")
        node = ctx.db.one("SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL", (name,))
        if not node:
            raise HTTPException(404, "machine is not registered")
        active = ctx.db.one("SELECT count(*) AS n FROM runs WHERE node=%s AND status='running'", (name,))
        if active and active["n"]:
            raise HTTPException(409, "machine has active sessions; wait for them to finish before removing it")
        with ctx.db.transaction() as connection:
            connection.execute("UPDATE nodes SET removed_at=now(),accepting_work=false,token_hash='',status='offline'"
                               " WHERE name=%s", (name,))
            connection.execute("UPDATE managed_ai_accounts SET removed_at=now(),updated_at=now() WHERE node=%s",
                               (name,))
            connection.execute("DELETE FROM accounts WHERE node=%s", (name,))
            connection.execute("UPDATE node_join_tokens SET used_at=now() WHERE node_name=%s AND used_at IS NULL",
                               (name,))
            fleet_routes.publish_for(ctx, NodeRemoved(node=name, actor=user.subject))
        ctx.events.emit("node.removed", node=name, outcome="removed", reason="removed from Machines and AI screen")
        return {"node": name, "removed": True}
    return remove_managed_node

def install_node_admin_routes(app, ctx, deps):
    acceptance = _register_set_node_work_acceptance(app, ctx, deps)
    remove = _register_remove_managed_node(app, ctx, deps)
    return SimpleNamespace(set_node_work_acceptance=acceptance, remove_managed_node=remove)
