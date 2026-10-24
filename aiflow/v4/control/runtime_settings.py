"""Owner-managed runtime env files and safe rolling service restarts (docs/v4/runtime-settings.md).

Secret values stay in root-owned env files on the control host or worker nodes. The control API returns only key
names, set state, timestamps, affected services, and operation status. Node operations use a fixed action and
validated paths/units; values are stripped from queued operation records as soon as a node reports completion.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from ..install.secrets import read_env, read_env_timestamps
from ..core.runtime_settings_crypto import encrypt_value
from .db import J

KEY = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
PROJECT = re.compile(r"[A-Z][A-Z0-9]{1,15}\Z")
VALUE = re.compile(r"[\x21-\x7e]{1,4096}\Z")
_RESERVED = {
    "JIRA_EMAIL", "JIRA_API_TOKEN", "GH_TOKEN", "O2_BASIC", "AIFLOW4_DATABASE_URL", "AIFLOW4_REDIS_URL",
    "AIFLOW4_JOIN_TOKEN", "AIFLOW4_OWNER_TOKEN", "AIFLOW4_REVIEWER_TOKEN", "AIFLOW4_CONSOLE_SESSION_SECRET",
    "AIFLOW4_CONSOLE_LOGIN", "AIFLOW4_PUBLIC_URL", "AIFLOW4_GITHUB_APP_DIR", "AIFLOW4_JIRA_SECRET_DIR",
    "POSTGRES_PASSWORD", "ZO_ROOT_USER_PASSWORD",
}
_SECRET_WORDS = ("TOKEN", "PASSWORD", "SECRET", "PRIVATE_KEY", "API_KEY", "CREDENTIAL", "BASIC")
# These values are read by control-loops for scheduled S3 backups. That process does not load runtime.env,
# so reject them here rather than accept a change that would appear successful but leave backup auth unchanged.
_UNSUPPORTED_CONTROL_KEYS = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}


class RuntimeSettingBody(BaseModel):
    model_config = {"extra": "forbid"}
    scope: Literal["secret", "project"]
    project: str = ""
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=4096)


def is_secret(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in _SECRET_WORDS)


def sanitize_inventory(raw: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Accept key metadata only from a node heartbeat; discard every field that could carry a value."""
    out: dict[str, list[dict[str, Any]]] = {}
    for project, rows in list(raw.items())[:200]:
        if not PROJECT.fullmatch(str(project)) or not isinstance(rows, list):
            continue
        items = []
        for row in rows[:500]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            if not KEY.fullmatch(name):
                continue
            changed = str(row.get("changed_at") or "")[:40]
            items.append({"name": name, "is_set": bool(row.get("is_set")), "changed_at": changed})
        out[str(project)] = items
    return out


def _file_rows(path: Path, scope: str, project: str = "") -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        return []
    values = read_env(path)
    timestamps = read_env_timestamps(path, values)
    return [{"scope": scope, "project": project, "name": key, "is_secret": is_secret(key),
             "is_set": bool(value), "changed_at": timestamps.get(key, ""), "nodes": []}
            for key, value in sorted(values.items()) if KEY.fullmatch(key)]


def _serves_project(ctx, node: dict, project: str) -> bool:
    try:
        allowed = ctx.repo().projects_for_node(node["name"], node.get("projects") or [], bool(node.get("dynamic")))
        return project in allowed
    except (KeyError, ValueError):
        return project in (node.get("projects") or [])


def _worker_units(ctx, node_name: str) -> list[str]:
    units: list[str] = []
    for account in ctx.repo().accounts_on(node_name):
        if account.paused:
            continue
        workers = max(1, int(account.parallel or 1))
        for index in range(workers):
            instance = account.id if index == 0 else f"{account.id}.{index + 1}"
            units.append(f"aiflow4-worker@{instance}.service")
    return sorted(set(units))


def _targets(ctx, scope: str, project: str = "") -> tuple[list[dict], list[str]]:
    now = datetime.now(timezone.utc)
    if scope == "secret":
        rows = ctx.db.all("SELECT name,status,token_hash,last_heartbeat,accepting_work,runtime_settings_public_key FROM nodes"
                          " WHERE role='control' AND removed_at IS NULL ORDER BY name")
        services = ["control-api-a", "control-api-b"]
        targets = []
        for node in rows:
            heartbeat = node.get("last_heartbeat")
            if (node.get("token_hash") and node.get("status") == "online" and heartbeat
                    and (now - heartbeat).total_seconds() <= 75):
                if not node.get("runtime_settings_public_key"):
                    raise HTTPException(409, f"control host {node['name']} needs a current node-agent heartbeat")
                targets.append({"name": node["name"], "units": services, "resume_accepting": True,
                                "runtime_settings_public_key": node["runtime_settings_public_key"]})
        if not targets:
            raise HTTPException(409, "the control host agent must be online to restart control services safely")
        return targets, services

    rows = ctx.db.all("SELECT name,role,status,token_hash,last_heartbeat,accepting_work,projects,dynamic,"
                      "runtime_settings_public_key"
                      " FROM nodes WHERE removed_at IS NULL ORDER BY name")
    configured = [row for row in rows if row.get("token_hash") and _serves_project(ctx, row, project)]
    targets = []
    services: list[str] = []
    for node in configured:
        heartbeat = node.get("last_heartbeat")
        if (node.get("status") != "online" or not heartbeat or (now - heartbeat).total_seconds() > 75
                or not node.get("accepting_work", True)):
            raise HTTPException(409, f"machine {node['name']} must be online and accepting work before this project can be changed")
        units = _worker_units(ctx, node["name"])
        if not node.get("runtime_settings_public_key"):
            raise HTTPException(409, f"machine {node['name']} needs a current node-agent heartbeat")
        targets.append({"name": node["name"], "units": units, "drain_work": bool(units),
                        "resume_accepting": bool(node.get("accepting_work", True)),
                        "runtime_settings_public_key": node["runtime_settings_public_key"]})
        services.extend(f"{node['name']}: {unit}" for unit in units)
    if not targets:
        raise HTTPException(409, "no online machine is currently registered to serve this project")

    # Keep one authoritative copy of UI-managed project settings on the control host. It is part of the encrypted
    # control backup and is used to seed replacement workers. Apply it through the same node-operation coordinator
    # so a worker failure also rolls this copy back.
    reported_services = sorted(set(services)) or [f"worker services on {target['name']}" for target in targets]
    control_nodes = [row for row in rows if row.get("role") == "control" and row.get("token_hash")]
    if not control_nodes:
        raise HTTPException(409, "the control host agent must be online to keep project settings in backup")
    control = control_nodes[0]
    heartbeat = control.get("last_heartbeat")
    if (control.get("status") != "online" or not heartbeat or (now - heartbeat).total_seconds() > 75
            or not control.get("runtime_settings_public_key")):
        raise HTTPException(409, f"control host {control['name']} must be online before project settings can be changed")
    existing = next((target for target in targets if target["name"] == control["name"]), None)
    if not existing:
        targets.append({"name": control["name"], "units": [], "drain_work": False,
                        "resume_accepting": bool(control.get("accepting_work", True)),
                        "runtime_settings_public_key": control["runtime_settings_public_key"]})
    return targets, reported_services


def _display_inventory(ctx) -> dict[str, Any]:
    secret_file = Path(ctx.settings.runtime_secret_file)
    rows = _file_rows(secret_file, "secret")
    projects_dir = Path(ctx.settings.project_env_dir)
    if projects_dir.is_dir():
        for path in sorted(projects_dir.glob("*.env")):
            project = path.stem
            if PROJECT.fullmatch(project):
                rows.extend(_file_rows(path, "project", project))
    project_rows: dict[tuple[str, str], dict] = {}
    for node in ctx.db.all("SELECT name,runtime_settings FROM nodes WHERE removed_at IS NULL ORDER BY name"):
        inventories = node.get("runtime_settings") or {}
        for project, entries in inventories.items():
            if not PROJECT.fullmatch(str(project)):
                continue
            for entry in entries:
                name = str(entry.get("name") or "")
                if not KEY.fullmatch(name):
                    continue
                item = project_rows.setdefault((project, name), {"scope": "project", "project": project,
                    "name": name, "is_secret": is_secret(name), "is_set": False, "changed_at": "", "nodes": []})
                item["is_set"] = item["is_set"] or bool(entry.get("is_set"))
                item["changed_at"] = max(item["changed_at"], str(entry.get("changed_at") or ""))
                item["nodes"].append(node["name"])
    # A project file on the control host may also appear in its own node heartbeat; merge the two metadata views.
    merged: dict[tuple[str, str, str], dict] = {}
    for item in rows + list(project_rows.values()):
        identity = (item["scope"], item["project"], item["name"])
        current = merged.setdefault(identity, {**item, "nodes": list(item["nodes"])})
        current["is_set"] = current["is_set"] or item["is_set"]
        current["changed_at"] = max(current["changed_at"], item["changed_at"])
        current["nodes"] = sorted(set(current["nodes"]) | set(item["nodes"]))
    rows = list(merged.values())
    rows.sort(key=lambda item: (item["scope"], item["project"], item["name"]))
    projects = sorted(ctx.repo().projects)
    return {"settings": rows, "projects": projects, "changes": _recent_changes(ctx),
            "secret_services": ["control-api-a", "control-api-b"],
            "project_services": {project: _project_services(ctx, project) for project in projects}}


def _project_services(ctx, project: str) -> list[str]:
    try:
        _machines, services = _targets(ctx, "project", project)
        return services
    except HTTPException:
        return []


def bootstrap_settings_for_node(ctx, node: str, projects: list[str], inventory: dict[str, list[dict]],
                                public_key: str, workers: list[dict]) -> list[dict]:
    """Return missing central project values encrypted for an idle node's first worker start."""
    if workers or not public_key:
        return []
    directory = Path(ctx.settings.project_env_dir)
    encrypted: list[dict] = []
    for project in projects:
        if not PROJECT.fullmatch(str(project)):
            continue
        pending = ctx.db.one("SELECT 1 AS active FROM node_operations WHERE node=%s"
                             " AND action='runtime-setting-project' AND status IN ('queued','running')"
                             " AND payload->>'project'=%s LIMIT 1", (node, project))
        if pending:
            continue
        path = directory / f"{project}.env"
        if path.is_symlink() or not path.is_file():
            continue
        values = read_env(path)
        current = {str(entry.get("name")): entry for entry in inventory.get(project, [])
                   if isinstance(entry, dict)}
        timestamps = read_env_timestamps(path, values)
        for key, value in sorted(values.items()):
            if not KEY.fullmatch(key) or not value:
                continue
            local = current.get(key)
            if local and local.get("is_set"):
                continue
            changed_at = timestamps.get(key, "")
            if not changed_at:
                continue
            context = f"runtime-settings-bootstrap:{node}:{project}:{key}:{changed_at}"
            try:
                envelope = encrypt_value(public_key, value, context)
            except (ValueError, TypeError):
                raise HTTPException(409, f"machine {node} has an invalid runtime-setting encryption key") from None
            encrypted.append({"project": project, "name": key, "changed_at": changed_at, "envelope": envelope})
    return encrypted


def _recent_changes(ctx) -> list[dict]:
    rows = ctx.db.all("SELECT payload->>'change_id' AS change_id,max(created_at) AS created_at"
                      " FROM node_operations WHERE action LIKE 'runtime-setting%%' AND payload ? 'change_id'"
                      " GROUP BY payload->>'change_id' ORDER BY max(created_at) DESC LIMIT 20")
    changes = []
    for row in rows:
        change_id = str(row.get("change_id") or "")
        if not change_id:
            continue
        try:
            changes.append(_change_status(ctx, change_id))
        except HTTPException:
            continue
    return changes


def _change_status(ctx, change_id: str) -> dict:
    rows = ctx.db.all("SELECT id,node,status,result,created_at,completed_at,payload->'services' AS services,"
                      "payload->>'phase' AS phase"
                      " FROM node_operations WHERE payload->>'change_id'=%s ORDER BY node", (change_id,))
    if not rows:
        raise HTTPException(404, "runtime setting operation not found")
    operations, rollbacks = [], []
    for row in rows:
        result = row.get("result") or {}
        # The encrypted pre-change file snapshot is a one-time node-to-node rollback capability. Never return it
        # from the owner API; only show the safe outcome fields needed by the Settings screen.
        safe_result = {key: result[key] for key in ("healthy", "key", "scope", "project", "services", "set",
                                                     "error", "failed_service", "cancelled") if key in result}
        payload = {"phase": row.get("phase") or "apply"}
        item = {"id": str(row["id"]), "node": row["node"], "status": row["status"],
                "result": safe_result, "services": row.get("services") or [],
                "completed_at": row["completed_at"].isoformat() if row.get("completed_at") else None}
        (rollbacks if payload.get("phase") == "rollback" else operations).append(item)

    apply_failures = [item for item in operations if item["status"] == "failed"
                      and not item["result"].get("cancelled")]
    apply_terminal = all(item["status"] in {"completed", "failed"} for item in operations)
    rollback_terminal = all(item["status"] in {"completed", "failed"} for item in rollbacks)
    if not apply_failures:
        status = "completed" if apply_terminal else "running"
        recovery = "not_needed"
    elif not apply_terminal or not rollback_terminal:
        status, recovery = "rolling_back", "pending"
    else:
        status = "failed"
        unrecovered_apply = any(any(marker in str(item["result"].get("error") or "")
                                    for marker in ("previous value could not be restored", "rollback health failed"))
                                for item in apply_failures)
        successful_nodes = {item["node"] for item in operations if item["status"] == "completed"}
        restored_nodes = {item["node"] for item in rollbacks
                          if item["status"] == "completed" and item["result"].get("healthy") is True}
        recovery = "restored" if not unrecovered_apply and successful_nodes <= restored_nodes else "failed"
    recovery_failure = next((item for item in operations if item["status"] == "failed" and not
                             item["result"].get("cancelled") and any(
                                 marker in str(item["result"].get("error") or "") for marker in
                                 ("previous value could not be restored", "rollback health failed"))), None)
    if recovery_failure is None:
        recovery_failure = next((item for item in rollbacks if item["status"] == "failed"), None)
    return {"id": change_id, "status": status, "recovery_status": recovery,
            "recovery_node": recovery_failure["node"] if recovery_failure else "",
            "recovery_error": recovery_failure["result"].get("error", "") if recovery_failure else "",
            "operations": operations, "rollbacks": rollbacks}


def reconcile_node_change(connection, change_id: str) -> None:
    """Coordinate a fleet change: keep project nodes drained until apply succeeds or every rollback finishes."""
    def rows():
        return connection.execute(
            "SELECT id,node,action,actor,status,payload,result FROM node_operations"
            " WHERE payload->>'change_id'=%s ORDER BY id FOR UPDATE", (change_id,)).fetchall()

    current = rows()
    applies = [row for row in current if (row["payload"] or {}).get("phase") != "rollback"]
    rollbacks = [row for row in current if (row["payload"] or {}).get("phase") == "rollback"]
    failures = [row for row in applies if row["status"] == "failed"
                and not (row["result"] or {}).get("cancelled")]

    if not failures:
        if applies and all(row["status"] == "completed" for row in applies):
            for row in applies:
                connection.execute("UPDATE node_operations SET result=result-'rollback_envelope' WHERE id=%s",
                                   (row["id"],))
            _resume_change_nodes(connection, applies)
        return

    # Operations not yet claimed must not apply the new value after a peer has failed. Mark them terminal so the
    # screen can explain the aborted fleet change; a node already running is allowed to finish and is rolled back
    # immediately when its completion arrives.
    for row in applies:
        if row["status"] == "queued":
            connection.execute("UPDATE node_operations SET status='failed',completed_at=now(),result=%s WHERE id=%s",
                               (J({"healthy": False, "cancelled": True,
                                   "error": "not applied because another machine failed"}), row["id"]))

    current = rows()
    applies = [row for row in current if (row["payload"] or {}).get("phase") != "rollback"]
    rollbacks = [row for row in current if (row["payload"] or {}).get("phase") == "rollback"]
    rollback_for = {str((row["payload"] or {}).get("rollback_for") or "") for row in rollbacks}
    for row in applies:
        result, payload = row["result"] or {}, row["payload"] or {}
        if row["status"] != "completed" or not result.get("healthy") or not result.get("rollback_envelope"):
            continue
        if str(row["id"]) in rollback_for:
            continue
        rollback_id = str(uuid.uuid4())
        rollback_context = f"{change_id}:rollback:{row['id']}"
        rollback_payload = {"action": "runtime_setting_rollback", "phase": "rollback",
                            "change_id": change_id, "rollback_for": str(row["id"]),
                            "scope": payload.get("scope"), "project": payload.get("project"),
                            "key": payload.get("key"), "rollback_context": rollback_context,
                            "rollback_envelope": result["rollback_envelope"],
                            "units": payload.get("units") or [], "services": payload.get("services") or [],
                            "resume_accepting": payload.get("resume_accepting", True)}
        connection.execute("INSERT INTO node_operations (id,node,action,actor,payload) VALUES (%s,%s,%s,%s,%s)",
                           (rollback_id, row["node"], "runtime-setting-rollback", row["actor"], J(rollback_payload)))
        # Move the encrypted recovery capability to the rollback operation so it is not retained twice.
        connection.execute("UPDATE node_operations SET result=result-'rollback_envelope' WHERE id=%s", (row["id"],))

    current = rows()
    applies = [row for row in current if (row["payload"] or {}).get("phase") != "rollback"]
    rollbacks = [row for row in current if (row["payload"] or {}).get("phase") == "rollback"]
    apply_terminal = all(row["status"] in {"completed", "failed"} for row in applies)
    rollback_terminal = all(row["status"] in {"completed", "failed"} for row in rollbacks)
    if not apply_terminal or not rollback_terminal:
        return
    rollback_ok = all(row["status"] == "completed" and (row["result"] or {}).get("healthy") is True
                      for row in rollbacks)
    apply_recovery_ok = not any(
        row["status"] == "failed" and any(
            marker in str((row["result"] or {}).get("error") or "")
            for marker in ("previous value could not be restored", "rollback health failed"))
        for row in applies)
    successful_have_rollback = all(
        row["status"] != "completed" or str(row["id"]) in rollback_for or
        any(str((rb["payload"] or {}).get("rollback_for") or "") == str(row["id"]) for rb in rollbacks)
        for row in applies)
    if rollback_ok and apply_recovery_ok and successful_have_rollback:
        _resume_change_nodes(connection, applies)


def _resume_change_nodes(connection, applies: list[dict]) -> None:
    resumed: dict[str, bool] = {}
    for row in applies:
        payload = row["payload"] or {}
        resumed[row["node"]] = bool(payload.get("resume_accepting", True))
    for name, accepting in resumed.items():
        connection.execute("UPDATE nodes SET accepting_work=%s,status=CASE WHEN %s THEN 'online' ELSE 'draining' END"
                           " WHERE name=%s", (accepting, accepting, name))


def install(app: FastAPI, ctx, console_user, audit) -> None:
    @app.get("/v1/runtime-settings")
    def runtime_settings_list(request: Request) -> dict:
        console_user(request, "console.manage")
        return _display_inventory(ctx)

    @app.post("/v1/runtime-settings", status_code=202)
    def runtime_settings_set(body: RuntimeSettingBody, request: Request) -> dict:
        user = console_user(request, "console.manage", target="runtime-settings", action="set",
                            node=f"{body.scope}:{body.project + '/' if body.project else ''}{body.key}")
        if not KEY.fullmatch(body.key) or not VALUE.fullmatch(body.value):
            raise HTTPException(422, "key or value has an unsupported format")
        if body.scope == "secret":
            # Single-quoted Compose env-file values keep dollar references literal. Quotes and backslashes are
            # excluded because the shared env-file readers intentionally do not interpret escape sequences.
            if "'" in body.value or "\\" in body.value:
                raise HTTPException(422, "control runtime values do not support quotes or backslashes")
            if body.project:
                raise HTTPException(422, "a runtime secret does not have a project name")
            if body.key in _RESERVED or body.key.startswith("AIFLOW4_"):
                raise HTTPException(422, "this key is reserved for AI Flow connection or installation settings")
            if body.key in _UNSUPPORTED_CONTROL_KEYS:
                raise HTTPException(422, "backup storage credentials are not supported by Runtime settings")
            targets, services = _targets(ctx, "secret")
            action = "runtime-setting-control"
        else:
            if not PROJECT.fullmatch(body.project) or body.project not in ctx.repo().projects:
                raise HTTPException(422, "choose a configured project")
            targets, services = _targets(ctx, "project", body.project)
            action = "runtime-setting-project"

        change_id = str(uuid.uuid4())
        op_ids = []
        with ctx.db.pool.connection() as connection:
            for target in targets:
                operation_id = str(uuid.uuid4())
                try:
                    value_envelope = encrypt_value(target["runtime_settings_public_key"], body.value, change_id)
                except (ValueError, TypeError):
                    raise HTTPException(409, f"machine {target['name']} has an invalid runtime-setting encryption key") from None
                payload = {"action": "runtime_setting_project" if body.scope == "project" else "runtime_setting_control",
                           "phase": "apply", "change_id": change_id, "scope": body.scope,
                           "project": body.project, "key": body.key, "value_envelope": value_envelope,
                           "units": target["units"], "services": services,
                           "resume_accepting": target["resume_accepting"]}
                connection.execute("INSERT INTO node_operations (id,node,action,actor,payload)"
                                   " VALUES (%s,%s,%s,%s,%s)",
                                   (operation_id, target["name"], action, user.subject, J(payload)))
                if body.scope == "project" and target.get("drain_work", True):
                    connection.execute("UPDATE nodes SET accepting_work=false,status='draining' WHERE name=%s",
                                       (target["name"],))
                op_ids.append({"id": operation_id, "node": target["name"]})
            connection.execute(
                "INSERT INTO write_audit (target,action,ticket,run_id,fence,actor,accepted,result,payload)"
                " VALUES ('runtime-settings','set',%s,%s,NULL,%s,true,'queued',%s)",
                (body.key, change_id, user.subject,
                 J({"scope": body.scope, "project": body.project, "key": body.key, "set": True,
                    "services": services, "change_id": change_id})))
        return {"change_id": change_id, "status": "queued", "key": body.key, "scope": body.scope,
                "project": body.project, "services": services, "operations": op_ids}

    @app.get("/v1/runtime-settings/changes/{change_id}")
    def runtime_settings_change(change_id: str, request: Request) -> dict:
        console_user(request, "console.manage")
        try:
            parsed = str(uuid.UUID(change_id))
        except ValueError:
            raise HTTPException(404, "runtime setting operation not found") from None
        return _change_status(ctx, parsed)
