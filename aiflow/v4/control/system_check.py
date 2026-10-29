"""Build, persist and alert on machine and project readiness results."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

from ..core.system_check import condition
from ..core.toolchains import (BASE_TOOLS, CONTAINER_RUNTIME_TOOL, PROVIDER_TOOLS, STACK_TOOLS, toolchain_gaps)
from .db import J
from . import notify
from .github import GITHUB_APP_REQUIRED_PERMISSIONS

BLOCKING = {"needs_action", "not_possible_on_this_machine"}
REQUIRED_PROJECT_STATUSES = ("Backlog", "Intake", "To Do", "In Progress", "In Verify", "Needs owner", "Done")
AUTO_FIXED_TOOL_IDS = frozenset(BASE_TOOLS) | frozenset(
    tool for tools in PROVIDER_TOOLS.values() for tool in tools
) | frozenset(tool for tools in STACK_TOOLS.values() for tool in tools)
MACHINE_CONDITION_TEXT = {
    "disk-space": ("Free disk space", {
        "ready": ("At least 5 GiB of free disk space is available.", ""),
        "fixed_automatically": ("The node freed disk space automatically.", ""),
        "needs_action": ("At least 5 GiB of free disk space is required.",
                          "Free at least 5 GiB on /srv/aiflow."),
        "not_possible_on_this_machine": ("Disk capacity cannot be checked on this machine.",
                                          "Upgrade the node agent so it can report disk space."),
    }),
    "workspace-permissions": ("Work directory permissions", {
        "ready": ("The node can write its work and state directories.", ""),
        "fixed_automatically": ("The node created its work directories automatically and can write its state.", ""),
        "needs_action": ("The node cannot write its work and state directories.",
                         "Restore write access to /srv/aiflow/work and /run/aiflow4/workers for the aiflow service account."),
        "not_possible_on_this_machine": ("Work directory permissions cannot be checked on this machine.",
                                          "Upgrade the node agent so it can check work directory permissions."),
    }),
    "control-network": ("Control API network", {
        "ready": ("The machine reached the control API and sent this heartbeat.", ""),
        "fixed_automatically": ("The node restored its control API connection automatically.", ""),
        "needs_action": ("The machine cannot reach the control API.",
                         "Restore the node's connection to the control API."),
        "not_possible_on_this_machine": ("Control API access cannot be checked on this machine.",
                                          "Upgrade the node agent so it can check control API access."),
    }),
    "repository-network": ("Repository network", {
        "ready": ("The machine can reach GitHub over HTTPS.", ""),
        "fixed_automatically": ("The node restored GitHub HTTPS access automatically.", ""),
        "needs_action": ("The machine cannot reach GitHub over HTTPS.",
                         "Allow outbound HTTPS access to github.com on port 443 from this machine."),
        "not_possible_on_this_machine": ("Repository access cannot be checked on this machine.",
                                          "Upgrade the node agent so it can check outbound HTTPS access to GitHub."),
    }),
}
MACHINE_CONDITION_IDS = set(MACHINE_CONDITION_TEXT)
MACHINE_CONDITION_IDS.add("node-heartbeat")


def _machine_condition(condition_id: str, result: object, node_status: str = "") -> dict[str, object] | None:
    """Rebuild a node condition from its allowlisted ID and result, never from node-supplied display text."""
    if not isinstance(result, str):
        return None
    if condition_id == "node-heartbeat":
        if result not in {"ready", "needs_action"}:
            return None
        if result == "ready":
            return condition(condition_id, "Node heartbeat", "ready",
                             "The node is connected to the control API.")
        return condition(condition_id, "Node heartbeat", "needs_action",
                         f"The node status is {node_status or 'not connected'}.",
                         "Start the node agent and restore its control API connection on this machine.")
    definition = MACHINE_CONDITION_TEXT.get(condition_id)
    if not definition or result not in definition[1]:
        return None
    name, outcomes = definition
    detail, action = outcomes[result]
    return condition(condition_id, name, result, detail, action)


def _validated_machine_results(raw: object) -> dict[str, str]:
    """Accept only known condition IDs and result states from a node; discard every other reported field."""
    if not isinstance(raw, list):
        return {}
    results: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        condition_id, result = item.get("id"), item.get("result")
        if isinstance(condition_id, str) and isinstance(result, str) \
                and condition_id in MACHINE_CONDITION_IDS and result in {
                "ready", "fixed_automatically", "needs_action", "not_possible_on_this_machine"}:
            results[condition_id] = result
    return results


def _credential_condition(account_id: str, status: dict[str, bool | None]) -> dict[str, object]:
    """Build a credential row from status booleans, never from agent-supplied display text."""
    name = f"Credentials · {account_id}"
    action = f"Sign in again to AI account {account_id} on this machine."
    if status.get("present") is False:
        return condition(f"credential:{account_id}", name, "needs_action", "Credential is missing.", action)
    if status.get("present") is True and status.get("valid") is False:
        return condition(f"credential:{account_id}", name, "needs_action", "Credential is invalid.", action)
    if status.get("present") is True and status.get("valid") is True:
        return condition(f"credential:{account_id}", name, "ready", "Credential is present and valid.")
    return condition(f"credential:{account_id}", name, "needs_action",
                     "Credential status has not been confirmed.",
                     f"Restart the AI account worker {account_id} on this machine and wait for its credential check to finish.")


def _account_condition(row: dict | None, account_id: str) -> dict[str, object]:
    if not row:
        return condition(f"credential:{account_id}", f"Credentials · {account_id}",
                         "needs_action", "Credential status has not been reported.",
                         f"Sign in to AI account {account_id} on this machine.")
    state = row.get("worker_state") or {}
    probe = state.get("probe") if isinstance(state, dict) else None
    if isinstance(probe, dict) and probe.get("ok") is True:
        return condition(f"credential:{account_id}", f"Credentials · {account_id}", "ready",
                         "Credential is present and valid.")
    if row.get("status") == "signed_out" or (isinstance(probe, dict) and probe.get("error_kind") == "auth"):
        return _credential_condition(account_id, {"present": True, "valid": False})
    return condition(f"credential:{account_id}", f"Credentials · {account_id}", "needs_action",
                     "Credential validity has not been confirmed.",
                     f"Sign in to AI account {account_id} on this machine.")


def _machine_conditions(raw: list[dict] | None, metrics: dict[str, Any],
                        auto_fixed_conditions: set[str], node_status: str) -> list[dict[str, object]]:
    reported = _validated_machine_results(raw)
    out: list[dict[str, object]] = []
    free = metrics.get("disk_free")
    disk_result = reported.get("disk-space")
    if isinstance(free, (int, float)) and free < 5 * 1024 ** 3:
        disk_result = "needs_action"
    elif disk_result is None:
        disk_result = ("fixed_automatically" if "disk-space" in auto_fixed_conditions else "ready") \
            if isinstance(free, (int, float)) and free >= 5 * 1024 ** 3 else "not_possible_on_this_machine"
    disk_condition = _machine_condition("disk-space", disk_result)
    if disk_condition:
        out.append(disk_condition)

    workspace = _machine_condition("workspace-permissions", reported.get("workspace-permissions"))
    if workspace is None:
        workspace = _machine_condition("workspace-permissions", "not_possible_on_this_machine")
    out.append(workspace)

    control_result = reported.get("control-network", "ready" if node_status == "online" else "needs_action")
    control = _machine_condition("control-network", control_result)
    if control:
        out.append(control)

    repo_result = reported.get("repository-network", "not_possible_on_this_machine")
    repository = _machine_condition("repository-network", repo_result)
    if repository:
        out.append(repository)

    if node_status == "online":
        out.append(_machine_condition("node-heartbeat", "ready", node_status))
    else:
        out.append(_machine_condition("node-heartbeat", "needs_action", node_status))
    return out


def _cached_integration_probe(ctx, key: tuple[object, ...], trigger: str, probe):
    """Cache one sanitized integration probe through an hour and deduplicate same-trigger fan-out."""
    cache = getattr(ctx, "_system_check_integration_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(ctx, "_system_check_integration_cache", cache)
    now = time.monotonic()
    entry = cache.get(key)
    if isinstance(entry, dict) and now - entry.get("at", 0) < 3600:
        # Heartbeats refresh machine conditions every 15 seconds. Event and hourly triggers recheck once, then
        # reuse that result while refresh_all fans the same check out across projects and machines.
        if trigger == "heartbeat" or (entry.get("trigger") == trigger and now - entry.get("at", 0) < 5):
            return entry.get("value")
    value = probe()
    cache[key] = {"at": now, "trigger": trigger, "value": value}
    return value


def _github_project_conditions(ctx, project, trigger: str, provider_checks: bool = True) -> list[dict[str, object]]:
    """Verify each configured repository through its assigned GitHub connection and check its base branch.

    Provider errors are deliberately reduced to fixed display text. GitHub response bodies can contain request
    details and must never become a readiness result.
    """
    repositories = [str(name) for name in project.configured_repositories if isinstance(name, str) and name]
    if not provider_checks:
        action = "Wait for the provider-backed System check to finish before starting work."
        pending = condition("github-installation", "GitHub installation", "needs_action",
                            "The GitHub installation check has not completed.", action)
        out = [pending, condition("github-app-permissions", "GitHub App permissions", "needs_action",
                                  "The GitHub permissions check has not completed.", action)]
        for repository in repositories:
            out.append(condition(f"repository-access:{repository}", f"Repository access · {repository}",
                                 "needs_action", "Repository access has not been verified.", action))
            out.append(condition(f"base-branch:{repository}", f"Base branch · {repository}", "needs_action",
                                 "The configured base branch has not been verified.", action))
        return out
    from .api import github_connection_list, github_connection_used, github_connections, github_gateway

    try:
        connections = github_connection_list(ctx)
        assignments = github_connections(ctx).assignments()
    except Exception:  # noqa: BLE001 - secret-store and provider details are not safe readiness output
        connections, assignments = [], {}

    by_id = {item.id: item for item in connections}
    owner_by_repository = {repository: repository.split("/", 1)[0].lower()
                           for repository in repositories if "/" in repository}
    selected: dict[str, tuple[object | None, str, set[str] | None]] = {}
    for repository in repositories:
        gateway, connection_id = None, ""
        try:
            gateway = github_gateway(ctx, repository)
            connection_id = github_connection_used.get()
        except Exception:  # noqa: BLE001 - provider response details are not safe readiness output
            assigned = assignments.get(repository.lower())
            connection = by_id.get(assigned) if assigned else next(
                (item for item in connections
                 if item.account.lower() == owner_by_repository.get(repository, "")), None)
            if connection is not None:
                gateway, connection_id = connection.gateway, connection.id

        accessible = None
        if gateway is not None:
            def read_connection_repositories() -> set[str] | None:
                try:
                    rows = gateway.installation_repositories()
                    return {str(item.get("full_name") or "").lower() for item in rows
                            if isinstance(item, dict) and item.get("full_name")}
                except Exception:  # noqa: BLE001 - provider details are not safe readiness output
                    return None

            accessible = _cached_integration_probe(
                ctx, ("github-repositories", connection_id), trigger, read_connection_repositories)
        selected[repository] = (gateway, connection_id, accessible)

    if repositories and all(accessible is not None for _, _, accessible in selected.values()):
        out = [condition("github-installation", "GitHub connection", "ready",
                         "AI Flow verified the GitHub connection selected for every configured repository.")]
    elif not repositories and connections:
        out = [condition("github-installation", "GitHub connection", "ready",
                         "A GitHub connection is available for this project.")]
    else:
        out = [condition("github-installation", "GitHub connection", "needs_action",
                         "A GitHub connection for this project could not be verified.",
                         "Connect GitHub or assign a working connection to each repository in the Projects screen.")]

    permission_labels = {"contents": "Contents", "pull_requests": "Pull requests", "workflows": "Workflows",
                         "actions": "Actions", "checks": "Checks", "statuses": "Statuses",
                         "administration": "Administration"}
    app_connections = {connection_id: gateway for gateway, connection_id, _ in selected.values()
                       if gateway is not None and getattr(gateway, "kind", "app") != "token"}
    permission_checks: list[dict[str, str] | None] = []
    for connection_id, gateway in app_connections.items():
        meta = getattr(gateway, "meta", None)
        installation = str(meta.get("installation_id") or "") if isinstance(meta, dict) else ""

        def read_installation_permissions(gateway=gateway,
                                         installation=installation) -> dict[str, str] | None:
            try:
                if not installation:
                    return None
                details = gateway.installation_details(int(installation))
                permissions = details.get("permissions") if isinstance(details, dict) else None
                return permissions if isinstance(permissions, dict) else None
            except Exception:  # noqa: BLE001 - provider response bodies are not safe readiness output
                return None

        permission_checks.append(_cached_integration_probe(
            ctx, ("github-permissions", connection_id, installation), trigger, read_installation_permissions))

    if not app_connections:
        if repositories and all(gateway is not None and getattr(gateway, "kind", "app") == "token"
                                for gateway, _, _ in selected.values()):
            out.append(condition("github-app-permissions", "GitHub App permissions", "ready",
                                 "This project uses verified GitHub token connections, so GitHub App permissions do not apply."))
        elif not repositories:
            out.append(condition("github-app-permissions", "GitHub App permissions", "ready",
                                 "No GitHub App permissions are required until a repository is configured."))
        else:
            out.append(condition("github-app-permissions", "GitHub App permissions", "needs_action",
                                 "GitHub App permissions could not be verified for the selected connection.",
                                 "Reconnect or assign a GitHub App connection in the Projects screen and rerun the System check."))
    elif any(permissions is None for permissions in permission_checks):
        out.append(condition("github-app-permissions", "GitHub App permissions", "needs_action",
                             "The GitHub App installation permissions could not be verified.",
                             "Reconnect the GitHub App on the Integrations screen and rerun the System check."))
    else:
        missing_permissions = {
            (permission_labels[permission], level)
            for permissions in permission_checks
            for permission, level in GITHUB_APP_REQUIRED_PERMISSIONS.items()
            if permissions.get(permission) != level
        }
        if missing_permissions:
            requirements = ", ".join(f"{name}: {level}" for name, level in sorted(missing_permissions))
            target = "the GitHub App installation" if len(app_connections) == 1 \
                else "each selected GitHub App installation"
            out.append(condition(
                "github-app-permissions", "GitHub App permissions", "needs_action",
                f"The GitHub App installation is missing these required permissions: {requirements}.",
                f"Grant {requirements} to {target} in GitHub settings."))
        else:
            out.append(condition("github-app-permissions", "GitHub App permissions", "ready",
                                 "Every selected GitHub App installation has the permissions AI Flow requires."))

    for repository in repositories:
        access_id = f"repository-access:{repository}"
        branch_id = f"base-branch:{repository}"
        repo_settings = project.settings_for_repo(repository)
        base = str(repo_settings.get("base") or "")
        gateway, connection_id, accessible = selected[repository]
        has_access = accessible is not None and repository.lower() in accessible
        if has_access:
            out.append(condition(access_id, f"Repository access · {repository}", "ready",
                                 "The selected GitHub connection can access the configured repository."))
        else:
            detail = "The selected GitHub connection cannot access the configured repository." \
                if accessible is not None else "Repository access could not be verified."
            action = "Assign a GitHub connection with access to this repository in the Projects screen."
            out.append(condition(access_id, f"Repository access · {repository}", "needs_action", detail, action))

        if not base:
            out.append(condition(branch_id, f"Base branch · {repository}", "needs_action",
                                 "A base branch is not configured for this repository.",
                                 "Configure a base branch for this repository in the Projects screen."))
        elif not has_access:
            out.append(condition(branch_id, f"Base branch · {repository}", "needs_action",
                                 "The configured base branch could not be checked without repository access.",
                                 "Assign a GitHub connection with access to this repository and rerun the System check."))
        else:
            def check_base_branch() -> bool | None:
                try:
                    return bool(gateway.branch_exists(repository.split("/", 1)[-1], base))
                except Exception:  # noqa: BLE001 - discard provider response bodies and request details
                    return None

            exists = _cached_integration_probe(ctx, ("github-branch", connection_id, repository.lower(), base),
                                               trigger, check_base_branch)
            if exists is True:
                out.append(condition(branch_id, f"Base branch · {repository}", "ready",
                                     "The configured base branch exists in the repository."))
            elif exists is False:
                out.append(condition(branch_id, f"Base branch · {repository}", "needs_action",
                                     "The configured base branch does not exist in the repository.",
                                     "Create the configured base branch in the repository and rerun the System check."))
            else:
                out.append(condition(branch_id, f"Base branch · {repository}", "needs_action",
                                     "The configured base branch could not be checked.",
                                     "Restore the selected GitHub connection and rerun the System check."))
    return out


def _check_jira_credential(ctx) -> dict[str, object]:
    """Check Jira credentials with a read-only request and expose only missing/valid/invalid state."""
    jira = getattr(ctx, "jira", None)
    if jira is None:
        return condition("jira-credentials", "Jira credentials", "needs_action", "Jira credential is missing.",
                         "Connect Jira on the Integrations screen.")

    auth = getattr(ctx, "jira_auth", None)
    if auth is not None:
        try:
            state = auth.state()
        except Exception:  # noqa: BLE001 - credential store details are not safe to display
            state = {}
        if isinstance(state, dict) and state.get("usable") is False:
            if state.get("reason") == "needs_reconnect":
                return condition("jira-credentials", "Jira credentials", "needs_action",
                                 "Jira credential is invalid.",
                                 "Reconnect Jira on the Integrations screen.")
            return condition("jira-credentials", "Jira credentials", "needs_action", "Jira credential is missing.",
                             "Connect Jira on the Integrations screen.")

    try:
        jira.projects()
    except Exception as exc:  # noqa: BLE001 - response text may contain tokens or other provider data
        from .jira import JiraError
        from .jira_connect import JiraNotConnected

        if isinstance(exc, JiraNotConnected):
            try:
                current_state = auth.state() if auth is not None else {}
            except Exception:  # noqa: BLE001 - credential store details are not safe to display
                current_state = {}
            if isinstance(current_state, dict) and current_state.get("reason") == "needs_reconnect":
                return condition("jira-credentials", "Jira credentials", "needs_action",
                                 "Jira credential is invalid.",
                                 "Reconnect Jira on the Integrations screen.")
            return condition("jira-credentials", "Jira credentials", "needs_action", "Jira credential is missing.",
                             "Connect Jira on the Integrations screen.")
        if isinstance(exc, JiraError):
            status = re.search(r"->\s*(\d{3})", str(exc))
            if status and status.group(1) == "401":
                return condition("jira-credentials", "Jira credentials", "needs_action",
                                 "Jira credential is invalid.",
                                 "Reconnect Jira on the Integrations screen.")
            if status and status.group(1) == "403":
                # Jira can reject project search for a valid account without Browse Projects access. The
                # project-scoped permission probe reports that permission separately below.
                return condition("jira-credentials", "Jira credentials", "ready",
                                 "Jira accepted the connected credential; project access is checked separately.")
        return condition("jira-credentials", "Jira credentials", "needs_action",
                         "Jira credential validity could not be confirmed.",
                         "Restore control-plane access to Jira and rerun the System check.")
    return condition("jira-credentials", "Jira credentials", "ready", "Jira credential is present and valid.")


def _jira_credential_condition(ctx, trigger: str) -> dict[str, object]:
    """Run a read-only Jira credential probe on scheduled checks and reuse its sanitized state per control plane."""
    site = str(getattr(ctx.settings, "jira_site", ""))
    return _cached_integration_probe(ctx, ("jira-credentials", site), trigger, lambda: _check_jira_credential(ctx))


def _jira_write_permission_condition(ctx, project_key: str, trigger: str,
                                     provider_checks: bool = True) -> dict[str, object]:
    """Check the Jira write permissions this project needs using Jira's read-only permissions endpoint."""
    jira = getattr(ctx, "jira", None)
    if jira is None or not jira:
        return condition("jira-write-permissions", f"Jira write permissions · {project_key}", "needs_action",
                         "Jira write permissions could not be checked because Jira is not connected.",
                         "Connect Jira on the Integrations screen and rerun the System check.")

    name = f"Jira write permissions · {project_key}"
    if not provider_checks:
        return condition("jira-write-permissions", name, "needs_action",
                         f"Jira permissions for project {project_key} have not been verified.",
                         "Wait for the provider-backed System check to finish before starting work.")

    site = str(getattr(ctx.settings, "jira_site", ""))
    try:
        permissions = _cached_integration_probe(
            ctx, ("jira-write-permissions", site, project_key), trigger,
            lambda: jira.project_write_permissions(project_key))
    except Exception:  # noqa: BLE001 - discard Jira response bodies and request details
        permissions = None

    if not isinstance(permissions, dict):
        return condition("jira-write-permissions", name, "needs_action",
                         "Jira write permissions could not be verified.",
                         "Restore control-plane access to Jira and rerun the System check.")

    required = {"BROWSE_PROJECTS": "Browse Projects", "CREATE_ISSUES": "Create Issues", "EDIT_ISSUES": "Edit Issues",
                "LINK_ISSUES": "Link Issues", "TRANSITION_ISSUES": "Transition Issues",
                "ADD_COMMENTS": "Add Comments"}
    missing = [label for permission, label in required.items() if permissions.get(permission) is not True]
    if missing:
        labels = " and ".join(missing)
        permission_word = "permission" if len(missing) == 1 else "permissions"
        return condition("jira-write-permissions", name, "needs_action",
                         f"The connected Jira account is missing {labels} {permission_word} in project {project_key}.",
                         f"Grant the connected Jira account {labels} {permission_word} in project {project_key}.")
    return condition(
        "jira-write-permissions", name, "ready",
        f"The connected Jira account can create, edit, link, transition and comment on issues in project {project_key}.")


def _jira_workflow_condition(ctx, project_key: str, trigger: str,
                             provider_checks: bool = True) -> dict[str, object]:
    """Keep Jira's required project workflow statuses in the ongoing work-readiness gate."""
    jira = getattr(ctx, "jira", None)
    name = f"Jira workflow statuses · {project_key}"
    if jira is None or not jira:
        return condition("jira-workflow-statuses", name, "needs_action",
                         "Required Jira workflow statuses could not be checked because Jira is not connected.",
                         "Connect Jira on the Integrations screen and rerun the System check.")
    if not provider_checks:
        return condition("jira-workflow-statuses", name, "needs_action",
                         f"Required Jira workflow statuses for {project_key} have not been verified.",
                         "Wait for the provider-backed System check to finish before starting work.")

    site = str(getattr(ctx.settings, "jira_site", ""))
    try:
        statuses = _cached_integration_probe(
            ctx, ("jira-workflow-statuses", site, project_key), trigger,
            lambda: jira.project_statuses(project_key))
    except Exception:  # noqa: BLE001 - provider output and request metadata stay out of readiness
        statuses = None
    if not isinstance(statuses, (list, tuple, set)):
        return condition("jira-workflow-statuses", name, "needs_action",
                         "Required Jira workflow statuses could not be verified.",
                         f"Restore control-plane access to Jira and rerun the System check for {project_key}.")
    available = {status for status in statuses if isinstance(status, str)}
    missing = [status for status in REQUIRED_PROJECT_STATUSES if status not in available]
    if missing:
        labels = ", ".join(missing)
        return condition("jira-workflow-statuses", name, "needs_action",
                         f"Jira project {project_key} is missing required workflow statuses: {labels}.",
                         f"Restore the required workflow statuses in Jira project {project_key} and rerun the System check.")
    return condition("jira-workflow-statuses", name, "ready",
                     f"Jira project {project_key} has all required workflow statuses.")


def _project_conditions(ctx, node: str, project_key: str, repo, tools: dict, metrics: dict,
                        auto_fixed_tools: set[str], credentials: dict[str, dict],
                        trigger: str = "heartbeat", provider_checks: bool = True) -> list[dict[str, object]]:
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
    repositories = [str(name) for name in project.configured_repositories if isinstance(name, str) and name]
    runtime_affects_all_work = project.containers or not repositories or all(
        project.settings_for_repo(name).get("containers") for name in repositories)
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
                                     f"Install or repair {tool} on node {node}.",
                                     affects_work=tool != CONTAINER_RUNTIME_TOOL or runtime_affects_all_work))
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
            detail = "The service has not reported healthy."
            out.append(condition(f"service:{service}", f"Service · {service}", "needs_action", detail,
                                 f"Start or repair the {service} service on node {node}."))
        elif state.get("state") in {"started", "recreated"}:
            out.append(condition(f"service:{service}", f"Service · {service}", "fixed_automatically",
                                 f"The {service} service was started automatically and is healthy."))
        else:
            out.append(condition(f"service:{service}", f"Service · {service}", "ready",
                                 f"The {service} service is healthy."))

    if not repositories or any(not project.settings_for_repo(name).get("base") for name in repositories):
        out.append(condition("repository-settings", "Repository settings", "needs_action",
                             "A repository and base branch are required to start work.",
                             f"Configure a repository and base branch for {project_key} in the Projects screen."))
    else:
        out.append(condition("repository-settings", "Repository settings", "ready",
                             "Repositories and base branch settings are configured."))

    out.extend(_github_project_conditions(ctx, project, trigger, provider_checks))
    if provider_checks:
        out.append(_jira_credential_condition(ctx, trigger))
    else:
        out.append(condition("jira-credentials", "Jira credentials", "needs_action",
                             "Jira credential validity has not been checked yet.",
                             "Wait for the provider-backed System check to finish before starting work."))
    out.append(_jira_write_permission_condition(ctx, project_key, trigger, provider_checks))
    out.append(_jira_workflow_condition(ctx, project_key, trigger, provider_checks))

    account_rows = {row["id"]: row for row in ctx.db.all(
        "SELECT id,status,worker_state FROM accounts WHERE node=%s ORDER BY id", (node,))}
    account_ids = [account.id for account in repo.accounts_on(node)]
    if not account_ids:
        out.append(condition("ai-account", "AI account", "not_possible_on_this_machine",
                             f"No AI account is assigned to node {node} for this project.",
                             f"Assign an AI account to project {project_key} on the Machines and AI screen."))
    else:
        for account_id in account_ids:
            out.append(credentials.get(account_id) or _account_condition(account_rows.get(account_id), account_id))
    return out


def _condition_map(readiness: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for item in readiness.get("machine", []) or []:
        if isinstance(item, dict):
            out[("machine", str(item.get("id")))] = item
    for project, items in (readiness.get("projects") or {}).items():
        for item in items or []:
            if isinstance(item, dict):
                out[(str(project), str(item.get("id")))] = item
    return out


def _validated_credential_statuses(raw: object, account_ids: list[str]) -> dict[str, dict[str, bool | None]]:
    """Keep only expected account IDs and explicit boolean presence/validity fields."""
    if not isinstance(raw, dict):
        return {}
    statuses: dict[str, dict[str, bool | None]] = {}
    for account_id in account_ids:
        value = raw.get(account_id)
        if not isinstance(value, dict) or "present" not in value or "valid" not in value:
            continue
        present, valid = value.get("present"), value.get("valid")
        if (present is not None and type(present) is not bool) or (valid is not None and type(valid) is not bool):
            continue
        statuses[account_id] = {"present": present, "valid": valid}
    return statuses


def _safe_readiness(readiness: object, account_ids: list[str] | None = None) -> dict:
    """Export only known snapshot fields and normalized conditions; discard raw node inputs."""
    if not isinstance(readiness, dict):
        return {}
    has_credential_snapshot = any(key in readiness for key in
                                  ("credential_inputs", "credential_checks", "credential_statuses"))
    safe: dict[str, Any] = {}
    checked_at = readiness.get("checked_at")
    if isinstance(checked_at, str) and len(checked_at) <= 64:
        safe["checked_at"] = checked_at
    trigger = readiness.get("trigger")
    if isinstance(trigger, str) and trigger in {
            "machine_joined", "project_added", "upgrade", "heartbeat", "hourly", "manual", "heartbeat_lost"}:
        safe["trigger"] = trigger
    credential_statuses = _validated_credential_statuses(readiness.get("credential_statuses"), account_ids or [])

    def clean_machine_rows(rows: object) -> list[dict[str, object]]:
        if not isinstance(rows, list):
            return []
        cleaned = []
        for item in rows:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            condition_id = item["id"]
            if condition_id not in MACHINE_CONDITION_IDS:
                continue
            normalized = _machine_condition(condition_id, item.get("result"),
                                            "online" if item.get("result") == "ready" else "offline")
            if normalized is not None:
                cleaned.append(normalized)
        return cleaned

    def clean_rows(rows: object) -> object:
        if not isinstance(rows, list):
            return []
        cleaned = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            condition_id = item.get("id")
            if not isinstance(condition_id, str):
                continue
            if condition_id.startswith("credential:"):
                account_id = condition_id.removeprefix("credential:")
                if account_ids is not None and account_id not in account_ids:
                    continue
                if account_id in credential_statuses:
                    cleaned.append(_credential_condition(account_id, credential_statuses[account_id]))
                elif item.get("result") == "ready":
                    cleaned.append(_credential_condition(account_id, {"present": True, "valid": True}))
                else:
                    # Older snapshots did not retain normalized status. Recheck until a fresh node report arrives.
                    cleaned.append(_credential_condition(account_id, {"present": None, "valid": None}))
                continue
            result = item.get("result")
            name, detail, action = item.get("name"), item.get("detail"), item.get("action", "")
            if (not isinstance(result, str)
                    or result not in {"ready", "fixed_automatically", "needs_action", "not_possible_on_this_machine"}
                    or not isinstance(name, str) or not isinstance(detail, str)
                    or not isinstance(action, str)):
                continue
            # Conditions outside the node's machine report are generated by the control plane. Keep only
            # their public display fields so legacy extension fields cannot leak through reports or bundles.
            cleaned.append({"id": condition_id, "name": name[:160], "result": result,
                            "detail": detail[:500], "action": action[:500],
                            "affects_work": item.get("affects_work") is not False})
        return cleaned

    if "machine" in readiness:
        safe["machine"] = clean_machine_rows(readiness.get("machine"))
    if isinstance(readiness.get("projects"), dict):
        clean_projects = {}
        for project, rows in readiness["projects"].items():
            if not isinstance(project, str) or len(project) > 32:
                continue
            if not isinstance(rows, list):
                clean_projects[project] = []
                continue
            if account_ids and has_credential_snapshot:
                existing = rows[-len(account_ids):] if len(rows) >= len(account_ids) else rows
                base = rows[:-len(account_ids)] if len(rows) >= len(account_ids) else []
                rebuilt = []
                for index, account_id in enumerate(account_ids):
                    old = existing[index] if index < len(existing) else {}
                    if account_id in credential_statuses:
                        rebuilt.append(_credential_condition(account_id, credential_statuses[account_id]))
                    elif isinstance(old, dict) and old.get("result") == "ready":
                        rebuilt.append(_credential_condition(account_id, {"present": True, "valid": True}))
                    else:
                        rebuilt.append(condition(f"credential:{account_id}", f"Credentials · {account_id}",
                                                 "needs_action", "Credential status has not been confirmed.",
                                                 f"Sign in to AI account {account_id} on this machine."))
                clean_projects[project] = clean_rows(base) + rebuilt
            else:
                clean_projects[project] = clean_rows(rows)
        safe["projects"] = clean_projects
    safe["credential_statuses"] = credential_statuses
    for field in ("auto_fixed_tools", "auto_fixed_conditions"):
        values = readiness.get(field)
        if isinstance(values, list):
            allowed = AUTO_FIXED_TOOL_IDS if field == "auto_fixed_tools" else MACHINE_CONDITION_IDS
            safe[field] = sorted({value for value in values if isinstance(value, str) and value in allowed})
    return safe


def refresh_node(ctx, node: str, trigger: str = "heartbeat", machine_conditions: list[dict] | None = None,
                 auto_fixed_tools: list[str] | None = None,
                 credential_checks: dict[str, dict] | None = None,
                 auto_fixed_conditions: list[str] | None = None,
                 provider_checks: bool = True) -> dict:
    """Re-evaluate a node from its latest heartbeat and configured projects; alert only on work-blocking changes."""
    row = ctx.db.one("SELECT name,status,tools,metrics,projects,dynamic,release_version,last_heartbeat,readiness"
                     " FROM nodes WHERE name=%s",
                     (node,))
    if not row:
        return {"node": node, "machine": [], "projects": {}, "checked_at": "", "trigger": trigger}
    repo = ctx.repo()
    assigned_accounts = [account.id for account in repo.accounts_on(node)]
    stored = _safe_readiness(row.get("readiness") or {}, assigned_accounts)
    raw_machine = machine_conditions if machine_conditions is not None else stored.get("machine", [])
    if trigger in {"heartbeat", "upgrade"}:
        # The heartbeat reaching this handler proves that the node can reach the control API. An older
        # agent may omit system_conditions, so do not carry a pre-heartbeat join result forward as stale.
        current_machine = _validated_machine_results(raw_machine)
        current_machine["control-network"] = "ready"
        raw_machine = [{"id": condition_id, "result": result}
                       for condition_id, result in current_machine.items()]
    credential_statuses = _validated_credential_statuses(
        credential_checks if credential_checks is not None else stored.get("credential_statuses"),
        assigned_accounts)
    auto_fixed = {item for item in (auto_fixed_tools if auto_fixed_tools is not None
                                    else stored.get("auto_fixed_tools", []))
                  if isinstance(item, str) and item in AUTO_FIXED_TOOL_IDS}
    auto_fixed_condition_ids = {item for item in (auto_fixed_conditions if auto_fixed_conditions is not None
                                                   else stored.get("auto_fixed_conditions", []))
                                if isinstance(item, str) and item in MACHINE_CONDITION_IDS}
    machine = _machine_conditions(raw_machine, row.get("metrics") or {}, auto_fixed_condition_ids,
                                  str(row.get("status") or "joined"))
    account_rows = {item["id"]: item for item in ctx.db.all(
        "SELECT id,status,worker_state FROM accounts WHERE node=%s ORDER BY id", (node,))}
    credentials = {
        account_id: _credential_condition(account_id, credential_statuses[account_id])
        if account_id in credential_statuses else _account_condition(account_rows.get(account_id), account_id)
        for account_id in assigned_accounts
    }
    project_keys = repo.projects_for_node(node, row.get("projects") or [], row.get("dynamic", False))
    project_results = {
        key: _project_conditions(ctx, node, key, repo, row.get("tools") or {}, row.get("metrics") or {},
                                 auto_fixed, credentials, trigger, provider_checks)
        for key in project_keys
    }
    if not provider_checks and trigger in {"heartbeat", "upgrade"}:
        # Heartbeats and upgrades refresh node-reported conditions before the background provider probe finishes.
        # Keep the last verified provider snapshot during that gap so an upgrade cannot erase a healthy baseline
        # and hide a newly revoked permission. With no baseline, the pending result remains fail-closed.
        provider_condition = lambda item: (
            item.get("id") in {"github-installation", "github-app-permissions", "jira-credentials",
                                "jira-write-permissions", "jira-workflow-statuses"}
            or str(item.get("id", "")).startswith(("repository-access:", "base-branch:"))
        )
        previous_projects = stored.get("projects") if isinstance(stored.get("projects"), dict) else {}
        for project_key, rows in project_results.items():
            old_rows = previous_projects.get(project_key, [])
            old_by_id = {str(item.get("id")): item for item in old_rows if isinstance(item, dict)}
            project_results[project_key] = [
                old_by_id.get(str(item.get("id")), item) if provider_condition(item) else item for item in rows
            ]
    checked_at = datetime.now(timezone.utc).isoformat()
    result = {"node": node, "machine": machine, "projects": project_results,
              "checked_at": checked_at, "trigger": trigger,
              "credential_statuses": credential_statuses,
              "auto_fixed_tools": sorted(auto_fixed),
              "auto_fixed_conditions": sorted(auto_fixed_condition_ids)}

    # A newer heartbeat or an online/offline transition can arrive while provider checks are running. Do not
    # let this older snapshot replace readiness built from the newer node state.
    current = ctx.db.one("SELECT status,last_heartbeat,readiness FROM nodes WHERE name=%s", (node,))
    if current and (current.get("status") != row.get("status")
                    or current.get("last_heartbeat") != row.get("last_heartbeat")):
        return current.get("readiness") or result

    previous = _condition_map(stored)
    current = _condition_map(result)
    for key, item in current.items():
        old = previous.get(key)
        changed = old is None or old.get("result") != item.get("result")
        provider_pending = not provider_checks and key[1] in {
            "github-installation", "github-app-permissions", "jira-credentials", "jira-write-permissions",
            "jira-workflow-statuses",
        } or (not provider_checks and key[1].startswith(("repository-access:", "base-branch:")))
        if (not changed or provider_pending or not item.get("affects_work") or item.get("result") not in BLOCKING
                or (trigger == "machine_joined" and row.get("last_heartbeat") is None)
                or key[1] == "node-heartbeat"):
            continue
        scope, condition_id = key
        title = f"System check: {node} · {item.get('name', condition_id)} needs attention"
        detail = str(item.get("action") or item.get("detail") or "Review this condition in System check.")
        notify.send("warning", title, detail, "/system-check", ctx=ctx)
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


def blocking_reasons(readiness: dict | None, project: str, account: str = "",
                     assigned_accounts: list[str] | None = None) -> list[dict]:
    """Return work-blocking failures, restricting account conditions to the requesting worker account."""
    safe = _safe_readiness(readiness, assigned_accounts)
    projects = safe.get("projects")
    if not safe.get("checked_at"):
        return [condition("system-check-snapshot", "System check", "needs_action",
                          "This machine has not completed its first system readiness check.",
                          "Wait for the machine's first heartbeat and system check before starting work.")]
    if not isinstance(projects, dict) or project not in projects:
        return [condition("project-check-snapshot", "Project system check", "needs_action",
                          f"No readiness result is available for project {project} on this machine.",
                          "Run the system check for this project before starting work.")]
    rows = list(safe.get("machine") or []) + list(projects.get(project) or [])
    return [item for item in rows if isinstance(item, dict) and item.get("result") in BLOCKING
            and item.get("affects_work", True)
            and (not str(item.get("id", "")).startswith("credential:")
                 or not account or str(item.get("id", "")) == f"credential:{account}")]


def project_node_readiness(readiness: dict | None, project: str, accounts: list[str]) -> dict[str, object]:
    """Summarize a node for Projects while keeping credential failures tied to their assigned accounts.

    A failed account is still shown in the returned conditions, but it blocks the machine only when every
    assigned account is unable to serve this project. Machine and project conditions apply to all accounts.
    """
    account_ids = list(dict.fromkeys(account for account in accounts if isinstance(account, str) and account))
    reasons = blocking_reasons(readiness, project, assigned_accounts=account_ids)
    global_reasons = [item for item in reasons if not str(item.get("id", "")).startswith("credential:")]
    failed_accounts = {
        str(item.get("id", "")).removeprefix("credential:")
        for item in reasons if str(item.get("id", "")).startswith("credential:")
    }
    account_reasons = [item for item in reasons
                       if str(item.get("id", "")).startswith("credential:")
                       and str(item.get("id", "")).removeprefix("credential:") in account_ids]
    blocked = bool(global_reasons) or not account_ids or all(account in failed_accounts for account in account_ids)
    return {"blocked": blocked, "conditions": global_reasons + account_reasons}


def report_nodes(ctx, project_scope: tuple[str, ...] | list[str] | None = None, *, include_release: bool = False) -> list[dict]:
    """Build safe system-check rows, limited to projects the requesting user may see when scoped."""
    columns = "name,status,readiness,readiness_checked_at,projects,dynamic"
    if include_release:
        columns += ",release_version"
    nodes = ctx.db.all(f"SELECT {columns} FROM nodes ORDER BY name")
    repo = ctx.repo()
    visible = set(project_scope) if project_scope is not None else None
    out = []
    for row in nodes:
        assigned = repo.projects_for_node(row["name"], row.get("projects") or [], row.get("dynamic", False))
        if visible is not None and not visible.intersection(assigned):
            continue
        readiness = _safe_readiness(row["readiness"] or {},
                                    [account.id for account in repo.accounts_on(row["name"])])
        if visible is not None and isinstance(readiness.get("projects"), dict):
            readiness["projects"] = {key: value for key, value in readiness["projects"].items() if key in visible}
        result = {"name": row["name"], "status": row["status"], "readiness": readiness,
                  "checked_at": row["readiness_checked_at"].isoformat() if row["readiness_checked_at"] else None}
        if include_release:
            result["release"] = row["release_version"]
        out.append(result)
    return out


def support_bundle(ctx, project_scope: tuple[str, ...] | list[str] | None = None) -> dict:
    """Safe, compact support snapshot with credential rows rebuilt from allowlisted status only."""
    visible = set(project_scope) if project_scope is not None else None
    projects = sorted(key for key in ctx.repo().projects if visible is None or key in visible)
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "system_check": {"nodes": report_nodes(ctx, project_scope, include_release=True)},
            "projects": projects}
