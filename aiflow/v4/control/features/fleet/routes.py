"""Routes of the fleet feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import re
import secrets
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

from ...system_check_api import HeartbeatBody
from .bodies import CheckProbeBody, CheckProbeTokenBody, ConfirmBody, JoinBody, JoinCommandBody, JoinInspectBody, JoinTokenBody, ManagedAccountCreateBody, ManagedAccountSignInStatusBody, ManagedAccountUpdateBody, NodeOperationBody, NodeOperationCheckpointBody, NodeOperationResultBody
from .events import (AiAccountAdded, AiAccountManagementChanged, AiAccountRemoved, AiAccountSignInCompleted,
                     AiAccountSignInRestarted, CheckProbeTokenIssued, NodeJoinCommandCreated, NodeJoined,
                     NodeOperationQueued, NodeRemoved, NodeWorkAcceptanceChanged)
from urllib.parse import urlsplit
from fastapi import Depends, HTTPException, Request
from ....core.controlrepo import Account, ControlRepo
from ....core.services import service_gaps
from ....core.toolchains import (CONTAINER_RUNTIME_TOOL, missing_tools, pinned_version, toolchain_gaps)
from ... import (flow_doctor_api, licensing, load_policy, load_policy_api,
               log_viewer, quota_refresh, runtime_settings, system_check_api, tooling)
from ... import environment as environment_reports
from ...db import J
from ...platform.events.wiring import dispatcher_for, publish_for
from ...route_deps import github_connection_used, h
from ...features.work.constants import utcnow


def install(app, ctx, deps) -> SimpleNamespace:
    from ...gateway_helpers import _issue_git_proxy_token, github_gateway, join_bootstrap_command, validated_join_control_url  # noqa: PLC0415 - api imports this module
    audit = deps.audit
    audit_in = deps.audit_in
    console_user = deps.console_user
    create_join_grant = deps.create_join_grant
    current_release = deps.current_release
    fleet_release = deps.fleet_release
    node_auth = deps.node_auth
    release_assets = deps.release_assets
    require_capacity = deps.require_capacity
    @app.post("/v1/nodes/join-tokens")
    def issue_join_token(body: JoinTokenBody) -> dict:
        if not ctx.settings.join_token or not secrets.compare_digest(body.provision_key, ctx.settings.join_token):
            raise HTTPException(403, "invalid provisioning key")
        token, expires_at = create_join_grant(body.name, body.role, body.project)
        ctx.events.emit("node.join_token_issued", node=body.name, reason=f"{body.role} · {body.project}")
        return {"token": token, "name": body.name, "role": body.role, "project": body.project,
                "expires_at": expires_at.isoformat()}

    @app.post("/v1/nodes/join-tokens/inspect")
    def inspect_join_token(body: JoinInspectBody) -> dict:
        row = ctx.db.one("SELECT node_name,role,project FROM node_join_tokens"
                         " WHERE token_hash=%s AND used_at IS NULL AND expires_at>now()", (h(body.join_token),))
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

    @app.post("/v1/nodes/join")
    def join(body: JoinBody) -> dict:
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
                envelope = publish_for(ctx, NodeJoined(node=row["name"], ip_present=bool(body.ip), role=row["role"],
                                                       project=row["projects"][0]))
        if not row:
            raise HTTPException(403, "invalid, expired, or used join token")
        dispatcher_for(ctx).run_once(event_id=envelope.id)  # the join's reactions run now, as they did inline
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

    @app.post("/v1/nodes/{name}/heartbeat")
    def node_heartbeat(name: str, body: HeartbeatBody, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        prev = ctx.db.one("SELECT status,accepting_work,release_version,tools,metrics FROM nodes WHERE name=%s", (node,))
        reported_tools, reported_release, reported_metrics, reported_services = system_check_api.reported_heartbeat(body, prev)
        inventory = runtime_settings.sanitize_inventory(body.runtime_settings)
        ctx.db.run("UPDATE nodes SET status=CASE WHEN accepting_work THEN 'online' ELSE 'draining' END,"
                   " slots_total=%s, slots_used=%s, release_version=%s, tools=%s,"
                   " metrics=%s, runtime_settings=%s, runtime_settings_public_key=%s,"
                   " release_protocol=%s, last_heartbeat=now(),"
                   " idle_since=CASE WHEN %s=0 THEN coalesce(idle_since,now()) ELSE NULL END WHERE name=%s",
                   (body.slots_total, body.slots_used, reported_release, J(reported_tools),
                    J({**reported_metrics, "services": reported_services}),
                    J(inventory), body.runtime_settings_public_key,
                    body.release_protocol, body.slots_used, node))
        load_policy.record_heartbeat(ctx, node)
        repo = ctx.repo()
        node_state = ctx.db.one("SELECT projects,dynamic,tools,metrics FROM nodes WHERE name=%s", (node,)) or {}
        environment_reports.confirm_node_environment(ctx, node, body.environment_items)
        tooling.resolve_available(ctx, node, node_state)
        tool_repairs = tooling.install_requests(ctx, node, node_state, repo)
        for account_id, expires_at in body.credential_expiries.items():
            # the account row keeps the earliest expiry of its machines; each machine's own date is kept beside it
            reported = repo.account(account_id)
            if not reported or not reported.runs_on(node):
                continue
            ctx.db.run("UPDATE account_machines SET credential_expires_at=%s WHERE account_id=%s AND node=%s",
                       (expires_at, account_id, node))
            ctx.db.run("UPDATE accounts SET credential_expires_at=(SELECT min(m.v) FROM (SELECT %s::timestamptz AS v"
                       " UNION ALL SELECT credential_expires_at FROM account_machines WHERE account_id=%s"
                       " AND node<>%s AND credential_expires_at IS NOT NULL) m) WHERE id=%s",
                       (expires_at, account_id, node, account_id))
        from ...sa import update_slot_pressure
        update_slot_pressure(ctx)
        heartbeat_status = "online" if not prev or prev.get("accepting_work", True) else "draining"
        ctx.events.ship("node.heartbeat", node=node, outcome=heartbeat_status, slots_total=body.slots_total,
                        slots_used=body.slots_used, release_version=body.release_version, metrics=body.metrics)
        if prev and prev["status"] != "online" and prev.get("accepting_work", True):
            ctx.events.emit("node.online", node=node, reason=f"was {prev['status']}")
            from ...incidents import close_incident
            close_incident(ctx, "node_down", node, "heartbeats are back")
        merged: dict[str, dict] = {}
        rank = {"working": 5, "probing": 4, "idle": 3, "unavailable": 2, "error": 1, "stopped": 0}
        for w in body.workers:
            acc = str(w.get("account", ""))
            if not acc:
                continue
            st = str(w.get("state", "unknown")) if w.get("alive", True) else "stopped"
            cur = merged.get(acc)
            if cur is None or rank.get(st, 0) > rank.get(cur["_state"], 0):
                merged[acc] = {**w, "_state": st, "instances": (cur or {}).get("instances", 0) + 1}
            else:
                cur["instances"] = cur.get("instances", 1) + 1
        for acc, w in merged.items():
            member = repo.account(acc)
            if not member or not member.runs_on(node):
                continue
            state = w.pop("_state")
            # "unavailable" (last probe failed) keeps the status the probe set: cooldown, signed_out or error
            ctx.db.run("UPDATE accounts SET worker_state=%s, status=CASE WHEN status='signed_out' THEN status"
                       " WHEN cooldown_until > now() THEN 'cooldown' WHEN %s = 'unavailable' THEN status"
                       " WHEN status = 'working' AND %s = 'idle' THEN status ELSE %s END,"
                       " updated_at=now() WHERE id=%s", (J(w), state, state, state, acc))
            ctx.db.run("UPDATE account_machines SET worker_state=%s WHERE account_id=%s AND node=%s",
                       (J({**w, "state": state}), acc, node))
            ctx.db.run("UPDATE accounts SET idle_since=CASE WHEN status='idle' THEN coalesce(idle_since,now()) ELSE NULL END,"
                       " cooldown_since=CASE WHEN status='cooldown' THEN coalesce(cooldown_since,now()) ELSE NULL END"
                       " WHERE id=%s", (acc,))
        repo = ctx.repo()
        sign_in_requests = ctx.db.all(
            "SELECT id,provider,sign_in_request_id,sign_in_status FROM managed_ai_accounts"
            " WHERE node=%s AND removed_at IS NULL AND sign_in_status IN ('queued','starting','waiting')"
            " ORDER BY created_at,id", (node,))
        signing_in = {row["id"] for row in sign_in_requests}
        # A shared account still waiting for sign-in on this machine gets no worker: its auth probe would fail and
        # reset the sign-in attempt (link and code) the user is completing. Workers start once sign-in completes.
        pending_shared = {row["account_id"] for row in ctx.db.all(
            "SELECT account_id FROM account_machines WHERE node=%s AND status='pending'", (node,))}
        assigned = [a for a in repo.accounts_on(node)
                    if not a.paused and a.id not in signing_in and a.id not in pending_shared]
        instances = [a.id if i == 1 else f"{a.id}.{i}" for a in assigned
                     for i in range(1, a.sessions + 1)]
        # Shared accounts are signed in independently on each machine. The node agent starts their provider CLI
        # login and reports only the provider's short-lived link and code; credentials stay in its account home.
        shared_signins = []
        for row in ctx.db.all("SELECT account_id, signin_attempt FROM account_machines WHERE node=%s"
                              " AND status='pending' ORDER BY account_id", (node,)):
            account = repo.account(row["account_id"])
            if account:
                shared_signins.append({"account": account.id, "provider": account.provider,
                                       "request_id": f"shared:{node}:{account.id}:{row['signin_attempt']}",
                                       "shared_account": True})
        # Legacy agents may execute an installer from an archive before verifying that archive. Keep their
        # automatic release target empty until a trusted signed-release verifier has been bootstrapped manually.
        # Signed-release agents follow the fleet rollout (canary, waves, rollback: docs/v4/fleet-upgrade.md).
        release = fleet_release(node, body.release_version) if body.release_protocol >= 1 else ""
        joined_node = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name=%s", (node,)) or {}
        node_projects = repo.projects_for_node(node, joined_node.get("projects") or [], joined_node.get("dynamic", False))
        project_stacks = {key: (repo.projects[key].stacks if repo.projects[key].repositories
                                else repo.projects[key].stack)
                          for key in node_projects if key in repo.projects}
        system_check_api.refresh_heartbeat(ctx, node, body, prev, reported_release)
        runtime_settings_bootstrap = runtime_settings.bootstrap_settings_for_node(
            ctx, node, node_projects, inventory, body.runtime_settings_public_key, body.workers)
        return {"desired": {"accounts": [a.id for a in assigned], "workers": instances,
                            "sign_ins": [{"account": row["id"], "provider": row["provider"],
                                          "request_id": row["sign_in_request_id"], "status": row["sign_in_status"]}
                                         for row in sign_in_requests], "signin": shared_signins,
                            "release": release,
                            "projects": node_projects,
                            "project_stacks": project_stacks,
                            "project_environment": {key: [item.name for item in repo.projects[key].environment_items
                                                           if item.secret]
                                                    for key in node_projects if key in repo.projects},
                            "project_services": {key: repo.projects[key].required_services for key in node_projects
                                                 if key in repo.projects and repo.projects[key].required_services},
                            "runtime_settings_bootstrap": runtime_settings_bootstrap,
                            "tool_repairs": tool_repairs,
                            "check_probes": tooling.pending_probes(ctx, node, node_projects),
                            "tools": repo.tools_for_node(node, node_projects), "versions": repo.versions}}

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
        gaps = missing_tools(tools, repo.tools_for_node(node, node_projects))
        for key in node_projects:
            project = repo.projects.get(key)
            if project and project.stacks:
                gaps += toolchain_gaps(tools, project.tools, repo.versions, project.stacks, require_pins=True)
        gaps = list(dict.fromkeys(gaps))
        # Declared test services (PostgreSQL, Redis) must be healthy too, or the node can claim no work.
        needed_services = list(dict.fromkeys(s for key in node_projects if key in repo.projects
                                             for s in repo.projects[key].required_services))
        reported_services = (row.get("metrics") or {}).get("services")
        unhealthy = service_gaps(reported_services, needed_services)
        age = row.get("age")
        return {"node": node, "status": row.get("status", "unknown"),
                "heartbeat_age_seconds": None if age is None else float(age),
                "projects": node_projects, "missing_tools": gaps, "missing_services": unhealthy,
                "preflight_ok": not gaps and not unhealthy}

    # --- owner-operated node installation and repair ---------------------------------------------------------
    def operation_plan(name: str, body: NodeOperationBody, node_row: dict) -> dict:
        repo = ctx.repo()
        configured = repo.nodes.get(name)
        packages = (configured.packages or []) if configured else []
        services = (configured.services or []) if configured else []
        if not isinstance(packages, list) or any(
                not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:_-]{0,127}", p)
                for p in packages):
            raise HTTPException(400, "node packages must be a list of safe package names")
        if not isinstance(services, list) or any(not isinstance(s, dict) for s in services):
            raise HTTPException(400, "node services must be a list of service definitions")
        for service in services:
            unit = service.get("unit")
            if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,180}\.service", unit):
                raise HTTPException(400, "node service unit is invalid")
            healthcheck = service.get("healthcheck", "")
            if not isinstance(healthcheck, str):
                raise HTTPException(400, "service health check must be an HTTP URL")
            if healthcheck:
                parsed = urlsplit(healthcheck)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                        or parsed.password or parsed.fragment):
                    raise HTTPException(400, "service health check must be an HTTP URL without embedded credentials")
        required = repo.tools_for_node(name)
        stacks = [stack for p in (configured.projects if configured else [])
                  if p in repo.projects for stack in repo.projects[p].stacks]
        missing = set(toolchain_gaps(node_row.get("tools") or {}, required, repo.versions, stacks,
                                     require_pins=bool(stacks)))
        selected: list[str]
        if body.action == "repair":
            if body.component:
                raise HTTPException(400, "repair reconciles the full desired state; omit component")
            selected = [tool for tool in required if tool != CONTAINER_RUNTIME_TOOL]
            if packages:
                selected.append("packages")
        else:
            component = body.component.strip()
            if component == "packages":
                if not packages:
                    raise HTTPException(400, "no OS packages are declared for this node")
                selected = [component]
            elif component:
                if component not in required:
                    raise HTTPException(400, "component is not required by this node's control repo state")
                if component == CONTAINER_RUNTIME_TOOL:
                    raise HTTPException(409, "configure rootless Podman and project quotas on the node")
                if component not in missing:
                    raise HTTPException(409, "component is already present on this node")
                selected = [component]
            else:
                selected = sorted(tool for tool in missing if tool != CONTAINER_RUNTIME_TOOL)
                if not selected and CONTAINER_RUNTIME_TOOL in missing:
                    raise HTTPException(409, "configure rootless Podman and project quotas on the node")
            if not selected:
                raise HTTPException(409, "node has no missing required components")

        # AI CLIs are installed with npm. Include Node.js in the server-authored plan so a clean node
        # receives the prerequisite before any CLI installation; the agent skips it when already healthy.
        if any(tool in {"claude", "codex", "grok", "opencode"} for tool in selected) and "node" not in selected:
            selected.append("node")

        tools = []
        for tool in selected:
            tools.append({"name": tool, "version": pinned_version(repo.versions, tool, stacks),
                          "packages": list(packages) if tool == "packages" else []})
        desired_services = list(services) if body.action == "repair" else []
        return {"action": body.action, "tools": tools, "services": desired_services}

    @app.post("/v1/nodes/{name}/join-command")
    def create_join_command(name: str, body: JoinCommandBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="node", action="join_command", node=name)
        if name != body.name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            audit("node", "join_command", name, "", None, user.subject, False, "invalid node name")
            raise HTTPException(400, "invalid node name")
        configured = ctx.repo().nodes.get(name)
        role = body.role or (configured.role if configured else "worker")
        project = body.project or (configured.projects[0] if configured and len(configured.projects) == 1 else "")
        if not project:
            audit("node", "join_command", name, "", None, user.subject, False, "project is required")
            raise HTTPException(422, "select a project for this node")
        try:
            control = validated_join_control_url(ctx.settings.join_control_url or ctx.settings.public_url)
        except ValueError as exc:
            audit("node", "join_command", name, "", None, user.subject, False, str(exc))
            raise HTTPException(503, "configure AIFLOW4_JOIN_CONTROL_URL to an address reachable from joining nodes") from exc
        version = current_release()
        try:
            release_assets(version)
        except HTTPException as exc:
            audit("node", "join_command", name, "", None, user.subject, False, "no signed release is published")
            raise HTTPException(409, "no signed release is published; run aiflow4-deploy first") from exc
        try:
            token, expires_at = create_join_grant(name, role, project)
        except HTTPException as exc:
            audit("node", "join_command", name, "", None, user.subject, False, str(exc.detail))
            raise
        publish_for(ctx, NodeJoinCommandCreated(node=name, actor=user.subject, expires_at=expires_at.isoformat(),
                                                role=role, project=project))
        command = join_bootstrap_command(token, control)
        ctx.events.emit("node.join_command.created", node=name, outcome="created",
                        reason="one-time token expires in five minutes", data={"actor": user.subject})
        return {"node": name, "role": role, "project": project, "command": command,
                "expires_at": expires_at.isoformat(), "expires_in_s": 300}

    JOB_ROLES = {"develop": "dev", "verify": "verify", "intake": "intake_refiner", "master": "master"}
    def account_jobs(roles: list[str]) -> list[str]:
        return [job for job, role in JOB_ROLES.items() if role in roles]

    def lock_machine_capacity(connection, name: str) -> None:
        """Serialize all account session-budget checks for this machine across API processes."""
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (f"aiflow4-machine-sessions:{name}",))

    def machine_capacity(repo: ControlRepo, name: str) -> int:
        return sum(max(1, int(account.parallel or 1)) for account in repo.accounts_on(name))

    def save_managed_account(account: Account, roles: list[str], sessions: int, paused: bool,
                             sign_in_url: str = "", sign_in_code: str = "",
                             expires_at: datetime | None = None, connection=None) -> None:
        sql = (
            "INSERT INTO managed_ai_accounts (id,provider,node,tier,roles,parallel,explicit_only,paused,"
            "sign_in_url,sign_in_code,sign_in_expires_at,sign_in_started_at,removed_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s<>'' THEN now() ELSE NULL END,NULL)"
            " ON CONFLICT (id) DO UPDATE SET provider=EXCLUDED.provider,node=EXCLUDED.node,tier=EXCLUDED.tier,"
            "roles=EXCLUDED.roles,parallel=EXCLUDED.parallel,explicit_only=EXCLUDED.explicit_only,paused=EXCLUDED.paused,"
            "sign_in_url=CASE WHEN EXCLUDED.sign_in_url='' THEN managed_ai_accounts.sign_in_url"
            " ELSE EXCLUDED.sign_in_url END,sign_in_code=CASE WHEN EXCLUDED.sign_in_code=''"
            " THEN managed_ai_accounts.sign_in_code ELSE EXCLUDED.sign_in_code END,"
            "sign_in_expires_at=coalesce(EXCLUDED.sign_in_expires_at,managed_ai_accounts.sign_in_expires_at),"
            "sign_in_started_at=CASE WHEN EXCLUDED.sign_in_code<>'' THEN now()"
            " ELSE managed_ai_accounts.sign_in_started_at END,"
            "removed_at=NULL,updated_at=now()")
        params = (account.id, account.provider, account.node, account.tier, roles, sessions, account.explicit_only,
                  paused, sign_in_url, sign_in_code, expires_at, sign_in_code)
        if connection is None:
            ctx.db.run(sql, params)
        else:
            connection.execute(sql, params)

    @app.post("/v1/machines/{name}/accounts")
    def add_managed_account(name: str, body: ManagedAccountCreateBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="add", node=name)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise HTTPException(422, "invalid machine name")
        roles = [JOB_ROLES[job] for job in dict.fromkeys(body.jobs)]
        request_id = str(uuid.uuid4())
        with ctx.db.transaction() as connection:
            lock_machine_capacity(connection, name)
            licensing.lock_resource_admission(connection)
            node = connection.execute("SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL", (name,)).fetchone()
            if not node:
                audit_in(connection, "ai-account", "add", name, "", None, user.subject, False,
                         "machine is not registered", {})
                raise HTTPException(404, "machine is not registered")
            repo = ctx.repo()
            capacity = machine_capacity(repo, name)
            if capacity + body.sessions > 8:
                audit_in(connection, "ai-account", "add", name, "", None, user.subject, False,
                         "machine session limit is eight",
                         {"current_sessions": capacity, "requested_sessions": body.sessions})
                raise HTTPException(422, "a machine can run at most 8 sessions")
            require_capacity("ai_accounts", connection=connection)
            existing_ids = {account.id for account in repo.accounts}
            existing_ids.update(row["id"] for row in connection.execute(
                "SELECT id FROM managed_ai_accounts").fetchall())
            index = 1
            while f"{body.provider}-{name}-{index:02d}" in existing_ids:
                index += 1
            account_id = f"{body.provider}-{name}-{index:02d}"
            connection.execute("INSERT INTO managed_ai_accounts (id,name,provider,node,tier,roles,parallel,paused,"
                               "sign_in_request_id,sign_in_status,sign_in_started_at)"
                               " VALUES (%s,%s,%s,%s,'middle',%s,%s,false,%s,'queued',now())",
                               (account_id, body.name.strip(), body.provider, name, roles, body.sessions, request_id))
            connection.execute("INSERT INTO accounts (id,provider,node,tier,roles,status)"
                               " VALUES (%s,%s,%s,'middle',%s,'signed_out')",
                               (account_id, body.provider, name, roles))
            publish_for(ctx, AiAccountAdded(account=account_id, actor=user.subject, node=name, provider=body.provider,
                                            name=body.name.strip(), sessions=body.sessions,
                                            jobs=account_jobs(roles)))
        ctx.events.emit("account.added", account=account_id, provider=body.provider, node=name,
                        outcome="sign_in_queued", reason="provider sign-in started on the machine")
        return {"account": account_id, "name": body.name.strip(), "node": name, "provider": body.provider,
                "sessions": body.sessions, "jobs": account_jobs(roles), "status": "queued", "sign_in_url": "", "code": ""}

    @app.patch("/v1/accounts/{account_id}")
    def update_managed_account(account_id: str, body: ManagedAccountUpdateBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="update", node=account_id)
        account = ctx.repo().account(account_id)
        if not account:
            raise HTTPException(404, "AI account is not registered")
        if body.sessions is None and body.jobs is None and body.action is None:
            raise HTTPException(422, "choose sessions, jobs, pause, or resume")
        with ctx.db.transaction() as connection:
            lock_machine_capacity(connection, account.node)
            repo = ctx.repo()
            account = repo.account(account_id)
            if not account:
                raise HTTPException(404, "AI account is not registered")
            sessions = body.sessions if body.sessions is not None else max(1, int(account.parallel or 1))
            total = machine_capacity(repo, account.node)
            if total - max(1, int(account.parallel or 1)) + sessions > 8:
                audit_in(connection, "ai-account", "update", account_id, "", None, user.subject, False,
                         "machine session limit is eight", {"current_sessions": total, "requested_sessions": sessions})
                raise HTTPException(422, "a machine can run at most 8 sessions")
            roles = ([JOB_ROLES[job] for job in dict.fromkeys(body.jobs)] if body.jobs is not None
                     else list(account.roles or []))
            paused = body.action == "pause" if body.action else account.paused
            save_managed_account(account, roles, sessions, paused, connection=connection)
            connection.execute("UPDATE accounts SET roles=%s,updated_at=now() WHERE id=%s", (roles, account_id))
            connection.execute("UPDATE shared_accounts SET roles=%s WHERE id=%s", (roles, account_id))
            action = body.action or "update"
            payload = {"sessions": sessions, "jobs": account_jobs(roles), "paused": paused}
            publish_for(ctx, AiAccountManagementChanged(account=account_id, actor=user.subject, action=action,
                                                        sessions=sessions, jobs=payload["jobs"], paused=paused))
        ctx.events.emit("account.management_changed", account=account_id, node=account.node,
                        outcome=action, reason="account settings updated", data=payload)
        return {"account": account_id, **payload}

    @app.post("/v1/accounts/{account_id}/sign-in")
    def restart_account_sign_in(account_id: str, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="sign_in_again", node=account_id)
        account = ctx.repo().account(account_id)
        if not account:
            raise HTTPException(404, "AI account is not registered")
        if ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' LIMIT 1", (account_id,)):
            raise HTTPException(409, "account has an active session; wait for it to finish before signing in again")
        request_id = str(uuid.uuid4())
        with ctx.db.transaction() as connection:
            save_managed_account(account, list(account.roles or []), max(1, int(account.parallel or 1)),
                                 account.paused, connection=connection)
            connection.execute("UPDATE managed_ai_accounts SET sign_in_request_id=%s,sign_in_status='queued',"
                               "sign_in_url='',sign_in_code='',sign_in_expires_at=NULL,sign_in_error='',"
                               "sign_in_started_at=now(),updated_at=now() WHERE id=%s",
                               (request_id, account_id))
            connection.execute("UPDATE accounts SET status='signed_out',last_error='',updated_at=now() WHERE id=%s",
                               (account_id,))
            publish_for(ctx, AiAccountSignInRestarted(account=account_id, actor=user.subject,
                                                      provider=account.provider, node=account.node))
        ctx.events.emit("account.sign_in_started", account=account_id, provider=account.provider,
                        node=account.node, outcome="queued", reason="owner requested provider sign-in again")
        return {"account": account_id, "status": "queued", "sign_in_url": "", "code": ""}

    @app.get("/v1/accounts/{account_id}/spec")
    def account_spec(account_id: str, node: str = Depends(node_auth)) -> dict:
        repo = ctx.repo()
        acc = repo.account(account_id)
        if not acc or not acc.runs_on(node):
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        model, effort = repo.model_for(acc.provider, acc.tier)
        return {"account": acc.id, "provider": acc.provider, "tier": acc.tier, "model": model, "effort": effort}

    @app.get("/v1/accounts/{account_id}/sign-in")
    def account_sign_in_details(account_id: str, request: Request) -> dict:
        console_user(request, {"nodes.manage"})
        row = ctx.db.one("SELECT node,provider,sign_in_status,sign_in_url,sign_in_code,sign_in_error,"
                         "sign_in_expires_at FROM managed_ai_accounts"
                         " WHERE id=%s AND removed_at IS NULL AND sign_in_status<>'idle'", (account_id,))
        if not row:
            raise HTTPException(404, "no active sign-in link for this account")
        return {"account": account_id, "node": row["node"], "provider": row["provider"],
                "status": row["sign_in_status"], "sign_in_url": row["sign_in_url"],
                "code": row["sign_in_code"], "error": row["sign_in_error"],
                "expires_at": row["sign_in_expires_at"].isoformat() if row["sign_in_expires_at"] else None}

    @app.post("/v1/accounts/{account_id}/sign-in/status")
    def report_account_sign_in_status(account_id: str, body: ManagedAccountSignInStatusBody,
                                      node: str = Depends(node_auth)) -> dict:
        row = ctx.db.one("SELECT node,sign_in_request_id,sign_in_status FROM managed_ai_accounts"
                         " WHERE id=%s AND removed_at IS NULL", (account_id,))
        if not row or row["node"] != node:
            raise HTTPException(404, "account sign-in request is not assigned to this machine")
        if row["sign_in_request_id"] != body.request_id:
            raise HTTPException(409, "sign-in request has been replaced")
        if body.sign_in_url:
            try:
                parsed = urlsplit(body.sign_in_url)
                _ = parsed.port
            except ValueError as exc:
                raise HTTPException(422, "provider sign-in URL is invalid") from exc
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(422, "provider sign-in URL must be an HTTPS URL")
        if body.code and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,79}", body.code):
            raise HTTPException(422, "provider sign-in code has an invalid format")
        if body.status == "waiting" and not body.sign_in_url:
            raise HTTPException(422, "provider sign-in must supply its authorization URL")
        with ctx.db.pool.connection() as connection:
            updated = connection.execute(
                "UPDATE managed_ai_accounts SET sign_in_status=%s,"
                "sign_in_url=CASE WHEN %s<>'' THEN %s ELSE sign_in_url END,"
                "sign_in_code=CASE WHEN %s<>'' THEN %s ELSE sign_in_code END,"
                "sign_in_error=%s,updated_at=now() WHERE id=%s AND node=%s AND sign_in_request_id=%s"
                " AND removed_at IS NULL RETURNING id",
                (body.status, body.sign_in_url, body.sign_in_url, body.code, body.code,
                 body.error[:300], account_id, node, body.request_id)).fetchone()
            if not updated:
                raise HTTPException(409, "sign-in request has been replaced")
            if body.status == "completed":
                quota_refresh.on_sign_in_completed(connection, account_id)
        return {"account": account_id, "status": body.status}

    @app.post("/v1/accounts/{account_id}/sign-in/complete")
    def complete_account_sign_in(account_id: str, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="sign_in_complete",
                            node=account_id)
        with ctx.db.transaction() as connection:
            row = connection.execute(
                "SELECT m.node,m.provider,m.sign_in_status,a.status,a.last_probe_at,m.sign_in_started_at"
                " FROM managed_ai_accounts m JOIN accounts a ON a.id=m.id"
                " WHERE m.id=%s AND m.removed_at IS NULL AND m.sign_in_status<>'idle'"
                " FOR UPDATE OF m", (account_id,)).fetchone()
            if not row:
                raise HTTPException(404, "no pending sign-in for this account")
            if row["sign_in_status"] != "completed":
                raise HTTPException(409, "provider authorization has not completed on the machine")
            if row["status"] not in ("idle", "working") or not row["last_probe_at"] or not row["sign_in_started_at"] \
                    or row["last_probe_at"] < row["sign_in_started_at"]:
                raise HTTPException(409, "the machine has not reported a successful account check since sign-in started")
            connection.execute("UPDATE managed_ai_accounts SET sign_in_url='',sign_in_code='',sign_in_error='',"
                               "sign_in_status='idle',sign_in_request_id='',sign_in_expires_at=NULL,"
                               "sign_in_started_at=NULL,updated_at=now() WHERE id=%s",
                               (account_id,))
            publish_for(ctx, AiAccountSignInCompleted(account=account_id, actor=user.subject, node=row["node"],
                                                      provider=row["provider"]))
        ctx.events.emit("account.sign_in_completed", account=account_id, provider=row["provider"],
                        node=row["node"], outcome="ready", reason="owner confirmed the successful provider check")
        return {"account": account_id, "signed_in": True}

    @app.delete("/v1/accounts/{account_id}")
    def remove_managed_account(account_id: str, body: ConfirmBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="remove", node=account_id)
        if not body.confirm:
            audit("ai-account", "remove", account_id, "", None, user.subject, False, "confirmation required")
            raise HTTPException(400, "confirm account removal")
        account = ctx.repo().account(account_id)
        if not account:
            raise HTTPException(404, "AI account is not registered")
        active = ctx.db.one("SELECT count(*) AS n FROM runs WHERE account_id=%s AND status='running'", (account_id,))
        if active and active["n"]:
            raise HTTPException(409, "account has an active session; wait for it to finish before removing it")
        with ctx.db.transaction() as connection:
            connection.execute("INSERT INTO managed_ai_accounts (id,provider,node,tier,roles,removed_at)"
                               " VALUES (%s,%s,%s,%s,%s,now()) ON CONFLICT (id) DO UPDATE SET removed_at=now(),"
                               "updated_at=now()", (account.id, account.provider, account.node, account.tier,
                                                     account.roles))
            connection.execute("DELETE FROM accounts WHERE id=%s", (account_id,))
            publish_for(ctx, AiAccountRemoved(account=account_id, actor=user.subject, node=account.node))
        ctx.events.emit("account.removed", account=account_id, provider=account.provider, node=account.node,
                        outcome="removed", reason="removed from Machines and AI screen")
        return {"account": account_id, "removed": True}

    load_policy_api.register_machine_load_limits(app, ctx, console_user, audit)

    @app.put("/v1/nodes/{name}/work-acceptance")
    def set_node_work_acceptance(name: str, body: ConfirmBody, accepting_work: bool, request: Request) -> dict:
        action = "resume_work" if accepting_work else "stop_taking_work"
        user = console_user(request, {"nodes.manage"}, target="node", action=action, node=name)
        if not accepting_work and not body.confirm:
            audit("node", action, name, "", None, user.subject, False, "confirmation required")
            raise HTTPException(400, "confirm stopping new work on this machine")
        row = ctx.db.one("SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL", (name,))
        if not row:
            raise HTTPException(404, "machine is not registered")
        with ctx.db.transaction():
            ctx.db.run("UPDATE nodes SET accepting_work=%s,status=CASE WHEN last_heartbeat > now()-interval '75 seconds'"
                       " THEN CASE WHEN %s THEN 'online' ELSE 'draining' END ELSE status END WHERE name=%s",
                       (accepting_work, accepting_work, name))
            publish_for(ctx, NodeWorkAcceptanceChanged(node=name, actor=user.subject,
                                                       accepting_work=accepting_work))
        ctx.events.emit("node.work_acceptance_changed", node=name, outcome=action,
                        reason="accepting new work" if accepting_work else "new work disabled")
        return {"node": name, "accepting_work": accepting_work}

    @app.delete("/v1/nodes/{name}")
    def remove_managed_node(name: str, body: ConfirmBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="node", action="remove", node=name)
        if not body.confirm:
            audit("node", "remove", name, "", None, user.subject, False, "confirmation required")
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
            publish_for(ctx, NodeRemoved(node=name, actor=user.subject))
        ctx.events.emit("node.removed", node=name, outcome="removed", reason="removed from Machines and AI screen")
        return {"node": name, "removed": True}

    flow_doctor_api.mount_node_routes(app, ctx, node_auth)

    @app.post("/v1/nodes/{name}/operations", status_code=202)
    def create_node_operation(name: str, body: NodeOperationBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="node", action=body.action, node=name)
        node_row = ctx.db.one("SELECT name, status, token_hash, tools, last_heartbeat"
                              " FROM nodes WHERE name=%s", (name,))
        if not node_row:
            audit("node", body.action, name, "", None, user.subject, False, "node is not registered")
            raise HTTPException(404, "node is not registered")
        heartbeat = node_row["last_heartbeat"]
        if (node_row["status"] != "online" or not node_row["token_hash"] or not heartbeat
                or (utcnow() - heartbeat).total_seconds() > 75):
            audit("node", body.action, name, "", None, user.subject, False, "node agent is not online")
            raise HTTPException(409, "node agent must be online to install or repair")
        try:
            payload = operation_plan(name, body, node_row)
        except HTTPException as exc:
            audit("node", body.action, name, "", None, user.subject, False, str(exc.detail))
            raise
        operation_id = str(uuid.uuid4())
        components = [str(t["name"]) for t in payload["tools"]]
        with ctx.db.transaction() as connection:
            connection.execute("INSERT INTO node_operations (id,node,action,actor,payload) VALUES (%s,%s,%s,%s,%s)",
                               (operation_id, name, body.action, user.subject, J(payload)))
            publish_for(ctx, NodeOperationQueued(node=name, actor=user.subject, action=body.action,
                                                 operation_id=operation_id, components=components,
                                                 service_count=len(payload["services"])))
        ctx.events.emit("node.operation.queued", node=name, outcome="queued",
                        reason=f"{body.action}: {', '.join(components) or 'services'}",
                        data={"operation_id": operation_id, "action": body.action, "actor": user.subject})
        return {"id": operation_id, "node": name, "action": body.action, "status": "queued",
                "components": components}

    @app.get("/v1/nodes/{name}/operations")
    def node_operations(name: str, request: Request) -> dict:
        console_user(request, {"console.read"})
        rows = ctx.db.all("SELECT id, node, action, actor, status, result, created_at, started_at, completed_at"
                          " FROM node_operations WHERE node=%s AND action <> 'read_logs'"
                          " ORDER BY created_at DESC LIMIT 50", (name,))
        for row in rows:
            row["id"] = str(row["id"])
        return {"node": name, "operations": rows}


    def probe_project(name: str, key: str):
        """The project when this machine serves it (a probe of anything else is refused)."""
        repo = ctx.repo()
        row = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name=%s", (name,)) or {}
        if key not in repo.projects_for_node(name, row.get("projects") or [], row.get("dynamic", False)):
            raise HTTPException(403, "the node does not serve this project")
        return repo.projects[key]

    @app.post("/v1/nodes/{name}/check-probes/token")
    def check_probe_token(name: str, body: CheckProbeTokenBody, node: str = Depends(node_auth)) -> dict:
        """Read-only token for the clean checkout on which a node runs a project's check commands."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        project = probe_project(name, body.project)
        if body.repo not in project.configured_repositories:
            raise HTTPException(403, "repository does not belong to the project")
        github = github_gateway(ctx, body.repo)
        connection_id = github_connection_used.get()
        if getattr(github, "kind", "app") == "token":
            expires = utcnow() + timedelta(hours=1)
            token = _issue_git_proxy_token(ctx, {
                "purpose": "check_probe", "connection": connection_id, "repo": body.repo.lower(),
                "node": node, "project": body.project, "expires": int(expires.timestamp()),
            })
            publish_for(ctx, CheckProbeTokenIssued(node=node, repo=body.repo, project=body.project,
                                                   connection=connection_id, expires=expires.isoformat(),
                                                   transport="control"))
            return {"token": token, "expires_at": expires.isoformat(),
                    "transport_url": f"{ctx.settings.control_url.rstrip('/')}/v1/github/git/{body.repo}.git"}
        token, expires = github.token(body.repo.split("/", 1)[-1], {"contents": "read", "metadata": "read"})
        publish_for(ctx, CheckProbeTokenIssued(node=node, repo=body.repo, project=body.project,
                                               connection=connection_id, expires=expires.isoformat(), transport=""))
        return {"token": token, "expires_at": expires.isoformat()}

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

    @app.post("/v1/nodes/{name}/operations/{operation_id}/checkpoint")
    def checkpoint_node_operation(name: str, operation_id: str, body: NodeOperationCheckpointBody,
                                  node: str = Depends(node_auth)) -> dict:
        """Persist the node's encrypted pre-change snapshot before a runtime setting can be written."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        try:
            parsed_id = str(uuid.UUID(operation_id))
        except ValueError as exc:
            raise HTTPException(404, "unknown node operation") from exc
        envelope = body.rollback_envelope
        if (set(envelope) != {"key", "nonce", "ciphertext"}
                or any(not value or len(value) > 2_000_000 for value in envelope.values())):
            raise HTTPException(422, "runtime setting snapshot is invalid")
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
                    (J({"rollback_envelope": envelope}), parsed_id, node)).fetchone()
                saved = updated["rollback_envelope"] if updated else None
        if not isinstance(saved, dict):
            raise HTTPException(409, "runtime setting snapshot could not be saved")
        return {"rollback_envelope": saved}

    @app.post("/v1/nodes/{name}/operations/{operation_id}/result")
    def finish_node_operation(name: str, operation_id: str, body: NodeOperationResultBody,
                              node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        try:
            parsed_id = str(uuid.UUID(operation_id))
        except ValueError as exc:
            raise HTTPException(404, "unknown node operation") from exc
        status = body.status
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
                (status, J(result), parsed_id, node))
            if operation["action"].startswith("runtime-setting") and payload.get("change_id"):
                runtime_settings.reconcile_node_change(connection, str(payload["change_id"]))
        updated = operation
        if not updated:
            raise HTTPException(404, "unknown or completed node operation")
        ctx.events.emit(f"node.operation.{status}", node=node, outcome=status,
                        reason=f"{updated['action']} finished", data={"operation_id": parsed_id,
                                                                     "healthy": result.get("healthy")})
        return {"id": parsed_id, "status": status}

    return SimpleNamespace(issue_join_token=issue_join_token, inspect_join_token=inspect_join_token, join=join, node_heartbeat=node_heartbeat, node_health=node_health, operation_plan=operation_plan, create_join_command=create_join_command, JOB_ROLES=JOB_ROLES, account_jobs=account_jobs, lock_machine_capacity=lock_machine_capacity, machine_capacity=machine_capacity, save_managed_account=save_managed_account, add_managed_account=add_managed_account, update_managed_account=update_managed_account, restart_account_sign_in=restart_account_sign_in, account_sign_in_details=account_sign_in_details, report_account_sign_in_status=report_account_sign_in_status, complete_account_sign_in=complete_account_sign_in, remove_managed_account=remove_managed_account, set_node_work_acceptance=set_node_work_acceptance, remove_managed_node=remove_managed_node, create_node_operation=create_node_operation, node_operations=node_operations, probe_project=probe_project, check_probe_token=check_probe_token, save_check_probe=save_check_probe, next_node_operation=next_node_operation, checkpoint_node_operation=checkpoint_node_operation, finish_node_operation=finish_node_operation)
