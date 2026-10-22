"""Build, persist and alert on machine and project readiness results."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.system_check import condition
from ..core.toolchains import STACK_TOOLS, toolchain_gaps
from .db import J
from . import notify

BLOCKING = {"needs_action", "not_possible_on_this_machine"}


def _account_condition(row: dict | None, account_id: str) -> dict[str, object]:
    if not row:
        return condition(f"credential:{account_id}", f"Credentials · {account_id}",
                         "needs_action", "Credential status has not been reported.",
                         f"Sign in to AI account {account_id} on this machine.")
    state = row.get("worker_state") or {}
    probe = state.get("probe") if isinstance(state, dict) else None
    if isinstance(probe, dict) and probe.get("ok"):
        return condition(f"credential:{account_id}", f"Credentials · {account_id}", "ready",
                         "Credential is present and valid.")
    if row.get("status") == "signed_out" or (isinstance(probe, dict) and probe.get("error_kind") == "auth"):
        return condition(f"credential:{account_id}", f"Credentials · {account_id}", "needs_action",
                         "Credential is invalid.", f"Sign in again to AI account {account_id} on this machine.")
    return condition(f"credential:{account_id}", f"Credentials · {account_id}", "needs_action",
                     "Credential validity has not been confirmed.",
                     f"Sign in to AI account {account_id} on this machine.", affects_work=False)


def _machine_conditions(raw: list[dict] | None, metrics: dict[str, Any],
                        auto_fixed_conditions: set[str], node_status: str) -> list[dict[str, object]]:
    out = [dict(item) for item in raw or [] if isinstance(item, dict)]
    known = {str(item.get("id")) for item in out}
    if node_status == "online":
        out.append(condition("node-heartbeat", "Node heartbeat", "ready",
                             "The node is connected to the control API."))
    else:
        out.append(condition("node-heartbeat", "Node heartbeat", "needs_action",
                             f"The node status is {node_status}.",
                             "Start the node agent and restore its control API connection on this machine."))
    free = metrics.get("disk_free")
    if "disk-space" not in known and isinstance(free, (int, float)):
        gib = int(free / (1024 ** 3))
        if free >= 5 * 1024 ** 3:
            result = "fixed_automatically" if "disk-space" in auto_fixed_conditions else "ready"
            detail = f"The node freed space automatically; {gib} GiB is available." if result == "fixed_automatically" \
                else f"{gib} GiB is available."
            out.append(condition("disk-space", "Free disk space", result, detail))
        else:
            out.append(condition("disk-space", "Free disk space", "needs_action",
                                 f"Only {gib} GiB is available; work needs at least 5 GiB.",
                                 "Free at least 5 GiB on /srv/aiflow."))
    elif "disk-space" not in known:
        out.append(condition("disk-space", "Free disk space", "needs_action",
                             "Disk capacity has not been reported.",
                             "Upgrade the node agent so it can report disk space."))

    if "workspace-permissions" not in known:
        out.append(condition("workspace-permissions", "Work directory permissions", "needs_action",
                             "Work directory permissions have not been checked by this node agent.",
                             "Upgrade the node agent so it can check work directory permissions."))
    if "control-network" not in known:
        out.append(condition("control-network", "Control API network", "ready",
                             "The machine reached the control API and sent this heartbeat."))
    if "repository-network" not in known:
        out.append(condition("repository-network", "Repository network", "needs_action",
                             "Repository network access has not been checked by this node agent.",
                             "Upgrade the node agent so it can check outbound HTTPS access to GitHub."))
    return out


def _project_conditions(ctx, node: str, project_key: str, repo, tools: dict, metrics: dict,
                        auto_fixed_tools: set[str], credentials: dict[str, dict]) -> list[dict[str, object]]:
    project = repo.projects.get(project_key)
    if not project:
        return []
    out: list[dict[str, object]] = []

    unknown_stacks = [stack for stack in project.stacks if stack not in STACK_TOOLS]
    if unknown_stacks:
        out.append(condition("stack-support", "Project stack", "not_possible_on_this_machine",
                             "This project uses an unsupported stack: " + ", ".join(unknown_stacks) + ".",
                             "Move this project to a machine with a supported stack or add stack support to AI Flow."))

    required_tools = repo.tools_for_node(node, [project_key])
    gaps = toolchain_gaps(tools, required_tools, repo.versions, project.stacks,
                          require_pins=bool(project.stacks))
    for tool in required_tools:
        if tool in gaps:
            stack_tool = any(tool in STACK_TOOLS.get(stack, ()) for stack in project.stacks)
            pin = (repo.versions.get("tools") or {}).get(tool) if isinstance(repo.versions, dict) else None
            if stack_tool and not pin:
                out.append(condition(f"tool:{tool}", f"Tool · {tool}", "needs_action",
                                     f"No pinned version is configured for {tool}.",
                                     f"Pin a supported {tool} version in versions.lock."))
            else:
                out.append(condition(f"tool:{tool}", f"Tool · {tool}", "needs_action",
                                     f"{tool} is missing, unhealthy, or outside its configured version.",
                                     f"Install or repair {tool} on node {node}."))
        elif tool in auto_fixed_tools:
            out.append(condition(f"tool:{tool}", f"Tool · {tool}", "fixed_automatically",
                                 f"{tool} was installed automatically and passed its version check."))
        else:
            out.append(condition(f"tool:{tool}", f"Tool · {tool}", "ready",
                                 f"{tool} is installed and matches its configured version."))

    services = (metrics.get("services") or {}) if isinstance(metrics, dict) else {}
    for service in project.required_services:
        state = services.get(service) if isinstance(services, dict) else None
        if not isinstance(state, dict) or not state.get("healthy"):
            detail = str((state or {}).get("detail") or (state or {}).get("state") or "Service has not reported healthy.")
            out.append(condition(f"service:{service}", f"Service · {service}", "needs_action", detail,
                                 f"Start or repair the {service} service on node {node}."))
        elif state.get("state") in {"started", "recreated"}:
            out.append(condition(f"service:{service}", f"Service · {service}", "fixed_automatically",
                                 f"The {service} service was started automatically and is healthy."))
        else:
            out.append(condition(f"service:{service}", f"Service · {service}", "ready",
                                 f"The {service} service is healthy."))

    repo_settings = project.settings_for_repo(project.repo_for(None))
    if not project.repo or not repo_settings.get("base"):
        out.append(condition("repository-settings", "Repository settings", "needs_action",
                             "A repository and base branch are required to start work.",
                             f"Configure a repository and base branch for {project_key} in the Projects screen."))
    else:
        out.append(condition("repository-settings", "Repository settings", "ready",
                             f"Repository {project.repo_for(None)} uses base branch {repo_settings['base']}."))

    try:
        from .api import github_gateway
        github = github_gateway(ctx)
        meta = getattr(github, "meta", None) if github else None
        github_connected = bool(github) and (not isinstance(meta, dict) or bool(meta.get("installation_id")))
    except Exception:  # noqa: BLE001 - the readiness result stays useful if secret-store metadata is unreadable
        github_connected = False
    if github_connected:
        out.append(condition("github-installation", "GitHub installation", "ready",
                             "AI Flow has a connected GitHub App installation."))
    else:
        out.append(condition("github-installation", "GitHub installation", "needs_action",
                             "The GitHub App installation is not connected.",
                             "Connect the GitHub App and select its installation on the Integrations screen."))

    account_rows = {row["id"]: row for row in ctx.db.all(
        "SELECT id,status,worker_state FROM accounts WHERE node=%s ORDER BY id", (node,))}
    account_ids = [account.id for account in repo.accounts_on(node)]
    if not account_ids:
        out.append(condition("ai-account", "AI account", "not_possible_on_this_machine",
                             f"No AI account is assigned to node {node} for this project.",
                             f"Assign an AI account to node {node} for project {project_key} in the control repo."))
    else:
        for account_id in account_ids:
            out.append(credentials.get(account_id) or _account_condition(account_rows.get(account_id), account_id))
    return out


def _condition_map(readiness: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for item in readiness.get("machine", []):
        if isinstance(item, dict):
            out[("machine", str(item.get("id")))] = item
    for project, items in (readiness.get("projects") or {}).items():
        for item in items or []:
            if isinstance(item, dict):
                out[(str(project), str(item.get("id")))] = item
    return out


def refresh_node(ctx, node: str, trigger: str = "heartbeat", machine_conditions: list[dict] | None = None,
                 auto_fixed_tools: list[str] | None = None,
                 credential_checks: dict[str, dict] | None = None,
                 auto_fixed_conditions: list[str] | None = None) -> dict:
    """Re-evaluate a node from its latest heartbeat and configured projects; alert only on work-blocking changes."""
    row = ctx.db.one("SELECT name,status,tools,metrics,projects,dynamic,release_version,last_heartbeat,readiness"
                     " FROM nodes WHERE name=%s",
                     (node,))
    if not row:
        return {"node": node, "machine": [], "projects": {}, "checked_at": "", "trigger": trigger}
    repo = ctx.repo()
    stored = row.get("readiness") or {}
    raw_machine = machine_conditions if machine_conditions is not None else stored.get("machine_inputs", [])
    credentials = credential_checks if credential_checks is not None else stored.get("credential_inputs", {})
    auto_fixed = set(auto_fixed_tools if auto_fixed_tools is not None else stored.get("auto_fixed_tools", []))
    auto_fixed_condition_ids = set(auto_fixed_conditions if auto_fixed_conditions is not None
                                   else stored.get("auto_fixed_conditions", []))
    machine = _machine_conditions(raw_machine, row.get("metrics") or {}, auto_fixed_condition_ids,
                                  str(row.get("status") or "joined"))
    project_keys = repo.projects_for_node(node, row.get("projects") or [], row.get("dynamic", False))
    project_results = {
        key: _project_conditions(ctx, node, key, repo, row.get("tools") or {}, row.get("metrics") or {},
                                 auto_fixed, credentials)
        for key in project_keys
    }
    checked_at = datetime.now(timezone.utc).isoformat()
    result = {"node": node, "machine": machine, "projects": project_results,
              "checked_at": checked_at, "trigger": trigger,
              "machine_inputs": raw_machine, "credential_inputs": credentials,
              "auto_fixed_tools": sorted(auto_fixed),
              "auto_fixed_conditions": sorted(auto_fixed_condition_ids)}

    previous = _condition_map(stored)
    current = _condition_map(result)
    for key, item in current.items():
        old = previous.get(key)
        changed = old is None or old.get("result") != item.get("result")
        if (not changed or not item.get("affects_work") or item.get("result") not in BLOCKING
                or (trigger == "machine_joined" and row.get("last_heartbeat") is None)
                or key[1] == "node-heartbeat"):
            continue
        scope, condition_id = key
        title = f"System check: {node} · {item.get('name', condition_id)} needs attention"
        detail = str(item.get("action") or item.get("detail") or "Review this condition in System check.")
        notify.send("warning", title, detail, "/system-check")
        ctx.events.emit("system_check.alert", project=scope if scope != "machine" else "", node=node,
                        outcome=str(item.get("result")), reason=detail,
                        data={"condition": condition_id, "trigger": trigger})

    ctx.db.run("UPDATE nodes SET readiness=%s, readiness_checked_at=now() WHERE name=%s", (J(result), node))
    if trigger != "heartbeat":
        ctx.events.emit("system_check.completed", node=node, outcome="checked", reason=trigger,
                        data={"projects": len(project_results), "conditions": len(current)})
    return result


def refresh_all(ctx, trigger: str = "hourly") -> list[dict]:
    return [refresh_node(ctx, row["name"], trigger)
            for row in ctx.db.all("SELECT name FROM nodes ORDER BY name")]


def blocking_reasons(readiness: dict | None, project: str, account: str = "") -> list[dict]:
    """Return work-blocking failures, restricting account conditions to the requesting worker account."""
    if not isinstance(readiness, dict):
        return []
    rows = list(readiness.get("machine") or []) + list((readiness.get("projects") or {}).get(project, []) or [])
    return [item for item in rows if isinstance(item, dict) and item.get("result") in BLOCKING
            and item.get("affects_work", True)
            and (not str(item.get("id", "")).startswith("credential:")
                 or not account or str(item.get("id", "")) == f"credential:{account}")]


def support_bundle(ctx) -> dict:
    """Safe, compact support snapshot; readiness data is already scrubbed of credential values."""
    nodes = ctx.db.all("SELECT name,status,release_version,tools,metrics,readiness,readiness_checked_at"
                       " FROM nodes ORDER BY name")
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "system_check": {"nodes": [
                {"name": row["name"], "status": row["status"], "release": row["release_version"],
                 "tools": row["tools"] or {}, "metrics": row["metrics"] or {},
                 "readiness": row["readiness"] or {},
                 "checked_at": row["readiness_checked_at"].isoformat() if row["readiness_checked_at"] else None}
                for row in nodes]},
            "projects": sorted(ctx.repo().projects)}
