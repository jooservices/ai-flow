"""Route installer groups for fleet.
"""
import uuid
from types import SimpleNamespace
from .bodies import NodeOperationCheckpointBody, NodeOperationResultBody
from fastapi import Depends, HTTPException
from ... import log_viewer, runtime_settings


def _parse_node_operation_id(operation_id: str) -> str:
    try:
        return str(uuid.UUID(operation_id))
    except ValueError as exc:
        raise HTTPException(404, "unknown node operation") from exc


def _validate_rollback_envelope(envelope: dict) -> None:
    if (set(envelope) != {"key", "nonce", "ciphertext"}
            or any(not value or len(value) > 2_000_000 for value in envelope.values())):
        raise HTTPException(422, "runtime setting snapshot is invalid")


def _write_node_operation_result(ctx, parsed_id, node, body, status):
    from . import routes as fleet_routes
    with ctx.db.pool.connection() as connection:
        candidate = connection.execute(
            "SELECT action,payload FROM node_operations WHERE id=%s AND node=%s AND status='running'",
            (parsed_id, node)).fetchone()
        if not candidate:
            raise HTTPException(404, "unknown or completed node operation")
        candidate_payload = candidate["payload"] or {}
        if candidate["action"].startswith("runtime-setting") and candidate_payload.get("change_id"):
            # Serialize every node result for a change before locking operation rows. This prevents two
            # completing nodes from each holding one row and deadlocking while the fleet coordinator locks
            # the whole change set.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                               (str(candidate_payload["change_id"]),))
        operation = connection.execute(
            "SELECT id,action,payload FROM node_operations WHERE id=%s AND node=%s AND status='running' FOR UPDATE",
            (parsed_id, node)).fetchone()
        if not operation:
            raise HTTPException(404, "unknown or completed node operation")
        payload = operation["payload"] or {}
        result = (log_viewer.safe_node_result(body.result, status) if operation["action"] == "read_logs"
                  else body.result)
        # Runtime values are delivered only to the node agent and removed from the durable queue record
        # as soon as it has applied or rolled them back. A successful project operation remains drained until
        # every serving node has applied the change, or the coordinator has rolled successful peers back.
        connection.execute(
            "UPDATE node_operations SET status=%s,result=%s,completed_at=now(),"
            " payload=CASE WHEN action LIKE 'runtime-setting%%'"
            " THEN payload - 'value_envelope' - 'rollback_envelope' ELSE payload END"
            " WHERE id=%s AND node=%s AND status='running'",
            (status, fleet_routes.J(result), parsed_id, node))
        if operation["action"].startswith("runtime-setting") and payload.get("change_id"):
            runtime_settings.reconcile_node_change(connection, str(payload["change_id"]))
    return operation, result


def _register_next_node_operation(app, ctx, deps, support=None):
    node_auth = deps.node_auth
    @app.get("/v1/nodes/{name}/operations/next")
    def next_node_operation(name: str, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        row = ctx.db.one(
            "WITH next AS (SELECT id FROM node_operations WHERE node=%s AND (status='queued' OR"
            " (status='running' AND started_at < now() - interval '30 minutes'))"
            " ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)"
            " UPDATE node_operations AS op SET status='running', started_at=now() FROM next"
            " WHERE op.id=next.id RETURNING op.id, op.action, op.payload", (node,))
        if not row:
            return {"operation": None}
        return {"operation": {"id": str(row["id"]), "action": row["action"], "payload": row["payload"]}}
    return next_node_operation

def _register_checkpoint_node_operation(app, ctx, deps, support=None):
    node_auth = deps.node_auth
    @app.post("/v1/nodes/{name}/operations/{operation_id}/checkpoint")
    def checkpoint_node_operation(name: str, operation_id: str, body: NodeOperationCheckpointBody,
                                  node: str = Depends(node_auth)) -> dict:
        """Persist the node's encrypted pre-change snapshot before a runtime setting can be written."""
        from . import routes as fleet_routes
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        parsed_id = _parse_node_operation_id(operation_id)
        envelope = body.rollback_envelope
        _validate_rollback_envelope(envelope)
        with ctx.db.pool.connection() as connection:
            operation = connection.execute(
                "SELECT action,payload FROM node_operations WHERE id=%s AND node=%s AND status='running' FOR UPDATE",
                (parsed_id, node)).fetchone()
            if (not operation or operation["action"] not in {"runtime-setting-control", "runtime-setting-project"}
                    or (operation["payload"] or {}).get("phase", "apply") != "apply"):
                raise HTTPException(404, "unknown or completed runtime setting operation")
            payload = operation["payload"] or {}
            saved = payload.get("rollback_envelope")
            if saved is None:
                updated = connection.execute(
                    "UPDATE node_operations SET payload=payload || %s WHERE id=%s AND node=%s AND status='running'"
                    " RETURNING payload->'rollback_envelope' AS rollback_envelope",
                    (fleet_routes.J({"rollback_envelope": envelope}), parsed_id, node)).fetchone()
                saved = updated["rollback_envelope"] if updated else None
        if not isinstance(saved, dict):
            raise HTTPException(409, "runtime setting snapshot could not be saved")
        return {"rollback_envelope": saved}
    return checkpoint_node_operation

def _register_finish_node_operation(app, ctx, deps, support=None):
    node_auth = deps.node_auth
    @app.post("/v1/nodes/{name}/operations/{operation_id}/result")
    def finish_node_operation(name: str, operation_id: str, body: NodeOperationResultBody,
                              node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        parsed_id = _parse_node_operation_id(operation_id)
        status = body.status
        operation, result = _write_node_operation_result(ctx, parsed_id, node, body, status)
        updated = operation
        if not updated:
            raise HTTPException(404, "unknown or completed node operation")
        ctx.events.emit(f"node.operation.{status}", node=node, outcome=status,
                        reason=f"{updated['action']} finished", data={"operation_id": parsed_id,
                                                                     "healthy": result.get("healthy")})
        return {"id": parsed_id, "status": status}
    return finish_node_operation

def install_node_operation_agent_routes(app, ctx, deps):
    next_operation = _register_next_node_operation(app, ctx, deps)
    checkpoint = _register_checkpoint_node_operation(app, ctx, deps)
    finish = _register_finish_node_operation(app, ctx, deps)
    return SimpleNamespace(next_node_operation=next_operation, checkpoint_node_operation=checkpoint,
                           finish_node_operation=finish)
