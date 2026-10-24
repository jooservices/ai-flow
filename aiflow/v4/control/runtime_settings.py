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

from ..install.secrets import read_env
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
    try:
        changed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        changed = ""
    return [{"scope": scope, "project": project, "name": key, "is_secret": is_secret(key),
             "is_set": bool(value), "changed_at": changed, "nodes": []}
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

    rows = ctx.db.all("SELECT name,status,token_hash,last_heartbeat,accepting_work,projects,dynamic,"
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
        targets.append({"name": node["name"], "units": units,
                        "resume_accepting": bool(node.get("accepting_work", True)),
                        "runtime_settings_public_key": node["runtime_settings_public_key"]})
        services.extend(f"{node['name']}: {unit}" for unit in units)
    if not targets:
        raise HTTPException(409, "no online machine is currently registered to serve this project")
    return targets, sorted(set(services)) or [f"worker services on {t['name']}" for t in targets]


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
    rows = ctx.db.all("SELECT id,node,status,result,created_at,completed_at,payload->'services' AS services"
                      " FROM node_operations WHERE payload->>'change_id'=%s ORDER BY node", (change_id,))
    if not rows:
        raise HTTPException(404, "runtime setting operation not found")
    operations = []
    for row in rows:
        result = row.get("result") or {}
        operations.append({"id": str(row["id"]), "node": row["node"], "status": row["status"],
                           "result": result, "services": row.get("services") or [],
                           "completed_at": row["completed_at"].isoformat() if row.get("completed_at") else None})
    failed = [row for row in operations if row["status"] == "failed"]
    complete = all(row["status"] in {"completed", "failed"} for row in operations)
    return {"id": change_id, "status": "failed" if failed else "completed" if complete else "running",
            "operations": operations}


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
            if body.project:
                raise HTTPException(422, "a runtime secret does not have a project name")
            if body.key in _RESERVED or body.key.startswith("AIFLOW4_"):
                raise HTTPException(422, "this key is reserved for AI Flow connection or installation settings")
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
                           "change_id": change_id, "scope": body.scope, "project": body.project,
                           "key": body.key, "value_envelope": value_envelope, "units": target["units"],
                           "services": services, "resume_accepting": target["resume_accepting"]}
                connection.execute("INSERT INTO node_operations (id,node,action,actor,payload)"
                                   " VALUES (%s,%s,%s,%s,%s)",
                                   (operation_id, target["name"], action, user.subject, J(payload)))
                if body.scope == "project":
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
