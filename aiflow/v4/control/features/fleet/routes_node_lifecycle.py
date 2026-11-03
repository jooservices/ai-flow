"""Route installer groups for fleet.
"""
import re
import secrets
from types import SimpleNamespace
from ...system_check_api import HeartbeatBody
from . import heartbeat
from .bodies import JoinBody, JoinInspectBody, JoinTokenBody
from .events import NodeJoined
from fastapi import Depends, HTTPException
from ... import licensing, load_policy, runtime_settings, system_check_api, tooling
from ... import environment as environment_reports
from ...db import J
from ...route_deps import h


def _node_preflight_gaps(repo, node, node_projects, tools, metrics):
    from . import routes as fleet_routes
    gaps = fleet_routes.missing_tools(tools, repo.tools_for_node(node, node_projects))
    for key in node_projects:
        project = repo.projects.get(key)
        if project and project.stacks:
            gaps += fleet_routes.toolchain_gaps(tools, project.tools, repo.versions, project.stacks,
                                                require_pins=True)
    gaps = list(dict.fromkeys(gaps))
    needed_services = list(dict.fromkeys(s for key in node_projects if key in repo.projects
                                         for s in repo.projects[key].required_services))
    unhealthy = fleet_routes.service_gaps((metrics or {}).get("services"), needed_services)
    return gaps, unhealthy


def _register_issue_join_token(app, ctx, deps, support=None):
    create_join_grant = deps.create_join_grant
    @app.post("/v1/nodes/join-tokens")
    def issue_join_token(body: JoinTokenBody) -> dict:
        if not ctx.settings.join_token or not secrets.compare_digest(body.provision_key, ctx.settings.join_token):
            raise HTTPException(403, "invalid provisioning key")
        token, expires_at = create_join_grant(body.name, body.role, body.project)
        ctx.events.emit("node.join_token_issued", node=body.name, reason=f"{body.role} · {body.project}")
        return {"token": token, "name": body.name, "role": body.role, "project": body.project,
                "expires_at": expires_at.isoformat()}
    return issue_join_token

def _register_inspect_join_token(app, ctx, deps, support=None):
    current_release = deps.current_release
    @app.post("/v1/nodes/join-tokens/inspect")
    def inspect_join_token(body: JoinInspectBody) -> dict:
        from . import routes as fleet_routes
        row = ctx.db.one("SELECT node_name,role,project FROM node_join_tokens"
                         " WHERE token_hash=%s AND used_at IS NULL AND expires_at>now()",
                         (fleet_routes.h(body.join_token),))
        if not row:
            raise HTTPException(403, "invalid, expired, or used join token")
        project = ctx.repo().projects.get(row["project"])
        if not project:
            raise HTTPException(409, "project is no longer configured")
        release = current_release()
        if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", release):
            raise HTTPException(503, "no signed AI Flow release is deployed")
        return {"name": row["node_name"], "role": row["role"], "project": row["project"],
                "stack": project.stack, "stacks": project.stacks, "release": release}
    return inspect_join_token

def _register_join(app, ctx, deps, support=None):
    current_release = deps.current_release
    @app.post("/v1/nodes/join")
    def join(body: JoinBody) -> dict:
        from . import routes as fleet_routes
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
        token = secrets.token_urlsafe(32)
        with ctx.db.transaction() as conn:
            row = conn.execute(
                "WITH grant_row AS (UPDATE node_join_tokens SET used_at=now()"
                " WHERE token_hash=%s AND used_at IS NULL AND expires_at>now()"
                " AND (%s::text IS NULL OR node_name=%s)"
                " RETURNING node_name,role,project,dynamic)"
                " INSERT INTO nodes (name,ip,role,token_hash,status,projects,dynamic)"
                " SELECT node_name,%s,role,%s,'joined',ARRAY[project],dynamic FROM grant_row"
                " ON CONFLICT (name) DO UPDATE SET token_hash=EXCLUDED.token_hash,"
                " ip=coalesce(nullif(EXCLUDED.ip,''),nodes.ip), role=EXCLUDED.role,"
                " projects=CASE WHEN nodes.removed_at IS NOT NULL THEN EXCLUDED.projects"
                " WHEN EXCLUDED.projects[1]=ANY(nodes.projects) THEN nodes.projects"
                " ELSE array_append(nodes.projects,EXCLUDED.projects[1]) END,"
                " dynamic=EXCLUDED.dynamic,status='joined',accepting_work=true,removed_at=NULL"
                " RETURNING name,role,projects,dynamic",
                (h(body.join_token), body.name, body.name, body.ip, h(token))).fetchone()
            envelope = None
            if row:
                envelope = fleet_routes.publish_for(ctx, NodeJoined(node=row["name"], ip_present=bool(body.ip), role=row["role"],
                                                       project=row["projects"][0]))
        if not row:
            raise HTTPException(403, "invalid, expired, or used join token")
        fleet_routes.dispatcher_for(ctx).run_once(event_id=envelope.id)  # the join's reactions run now, as they did inline
        repo = ctx.repo()
        project = repo.projects[row["projects"][0]]
        sign_in_requests = ctx.db.all(
            "SELECT id FROM managed_ai_accounts"
            " WHERE node=%s AND removed_at IS NULL AND sign_in_status IN ('queued','starting','waiting')",
            (row["name"],))
        signing_in = {account["id"] for account in sign_in_requests}
        accounts = repo.accounts_on(row["name"])
        return {"node": row["name"], "role": row["role"], "project": row["projects"][0],
                "stack": project.stack, "stacks": project.stacks, "release": current_release(), "node_token": token,
                "accounts": [a.id for a in accounts],
                "worker_accounts": [a.id for a in accounts if not a.paused and a.id not in signing_in]}
    return join

def _register_node_heartbeat(app, ctx, deps, support=None):
    fleet_release = deps.fleet_release
    node_auth = deps.node_auth
    @app.post("/v1/nodes/{name}/heartbeat")
    def node_heartbeat(name: str, body: HeartbeatBody, node: str = Depends(node_auth)) -> dict:
        from . import routes as fleet_routes
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        prev = ctx.db.one("SELECT status,accepting_work,release_version,tools,metrics FROM nodes WHERE name=%s", (node,))
        reported_tools, reported_release, reported_metrics, reported_services = system_check_api.reported_heartbeat(body, prev)
        inventory = runtime_settings.sanitize_inventory(body.runtime_settings)
        heartbeat.update_node_heartbeat_row(ctx, node, body, reported_tools, reported_release,
                                            reported_metrics, reported_services, inventory, fleet_routes.J)
        load_policy.record_heartbeat(ctx, node)
        repo = ctx.repo()
        node_state = ctx.db.one("SELECT projects,dynamic,tools,metrics FROM nodes WHERE name=%s", (node,)) or {}
        environment_reports.confirm_node_environment(ctx, node, body.environment_items)
        tooling.resolve_available(ctx, node, node_state)
        tool_repairs = tooling.install_requests(ctx, node, node_state, repo)
        heartbeat.persist_heartbeat_credentials(ctx, node, body, repo, J)
        from ...sa import update_slot_pressure
        update_slot_pressure(ctx)
        heartbeat_status = "online" if not prev or prev.get("accepting_work", True) else "draining"
        ctx.events.ship("node.heartbeat", node=node, outcome=heartbeat_status, slots_total=body.slots_total,
                        slots_used=body.slots_used, release_version=body.release_version, metrics=body.metrics)
        if prev and prev["status"] != "online" and prev.get("accepting_work", True):
            ctx.events.emit("node.online", node=node, reason=f"was {prev['status']}")
            from ...incidents import close_incident
            close_incident(ctx, "node_down", node, "heartbeats are back")
        merged = heartbeat.merge_worker_reports(body.workers)
        heartbeat.persist_worker_reports(ctx, node, merged, repo, fleet_routes.J)
        repo = ctx.repo()
        return heartbeat.desired_heartbeat_state(
            ctx, node, body, prev, reported_release, inventory, repo, tool_repairs,
            tooling, system_check_api, runtime_settings, fleet_release)
    return node_heartbeat

def _register_node_health(app, ctx, deps, support=None):
    node_auth = deps.node_auth
    @app.get("/v1/nodes/{name}/health")
    def node_health(name: str, node: str = Depends(node_auth)) -> dict:
        """What an installer needs to call a new node healthy: a fresh heartbeat and a green project preflight."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        row = ctx.db.one("SELECT status,tools,projects,dynamic,metrics,"
                         " extract(epoch FROM now()-last_heartbeat) AS age FROM nodes WHERE name=%s", (node,)) or {}
        repo = ctx.repo()
        node_projects = repo.projects_for_node(node, row.get("projects") or [], row.get("dynamic", False))
        tools = row.get("tools") or {}
        gaps, unhealthy = _node_preflight_gaps(repo, node, node_projects, tools, row.get("metrics"))
        age = row.get("age")
        return {"node": node, "status": row.get("status", "unknown"),
                "heartbeat_age_seconds": None if age is None else float(age),
                "projects": node_projects, "missing_tools": gaps, "missing_services": unhealthy,
                "preflight_ok": not gaps and not unhealthy}
    return node_health

def install_node_lifecycle_routes(app, ctx, deps):
    handlers = SimpleNamespace(
        issue_join_token=_register_issue_join_token(app, ctx, deps),
        inspect_join_token=_register_inspect_join_token(app, ctx, deps),
        join=_register_join(app, ctx, deps),
        node_heartbeat=_register_node_heartbeat(app, ctx, deps),
        node_health=_register_node_health(app, ctx, deps),
    )
    return handlers
