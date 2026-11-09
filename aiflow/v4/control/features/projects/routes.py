"""Routes of the projects feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import re
import html
import json
import httpx
from pathlib import Path
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace

from .bodies import EnvironmentRemovalBody, GitHubConnectionTokenBody, GitHubInstallationBody, ProjectEnvironmentBody, ProjectPauseBody, ProjectPreflightBody, ProjectRepositoriesBody, ProjectServicesBody
from .events import (EnvironmentRemovalsRecorded, GitHubAppConnected, GitHubAppImportVerified,
                     GitHubAppInstallOpened, GitHubConnectionAssigned, GitHubConnectionRemoved,
                     GitHubInstallationSettingsOpened, GitHubManifestCreated, GitHubManifestStarted,
                     GitHubRepositoryAccessChanged, GitHubRepositorySnapshotRecorded,
                     GitHubTokenConnectionAdded, GitHubTokenConnectionReplaced, ProjectAdded,
                     ProjectEnvironmentSaved, ProjectEnvironmentScanned, ProjectPauseChanged,
                     ProjectRemoved, ProjectRepositoriesSaved, ProjectServicesChanged)
from typing import Any, Callable
from urllib.parse import quote, urlsplit
import yaml
from fastapi import Depends, HTTPException, Request
from fastapi.responses import (HTMLResponse, RedirectResponse)
from ....core import work_limits
from ....core.services import normalize_services
from ....core.toolchains import (CONTAINER_RUNTIME_TOOL, STACK_TOOLS, missing_tools, toolchain_gaps,
                               tools_for_stack)
from ....core.environment import KINDS as ENV_KINDS, RUN_STEPS as ENV_RUN_STEPS, parse_items as parse_env_items
from ... import (licensing, system_check, system_check_api, tooling)
from ... import environment as environment_reports
from ...platform.events.wiring import publish_for
from ...github import GitHubApp
from ...github_connect import GitHubSecretStore, app_metadata
from ...github_connections import DEFAULT_ID, Connection, verify_token
from ... import inventory
from aiflow.v4.control.features.tickets import ticket_repository_of


# Keep the original module-level import surface available to callers and tests.
__all__ = [
    'Any',
    'CONTAINER_RUNTIME_TOOL',
    'Callable',
    'Connection',
    'DEFAULT_ID',
    'Depends',
    'ENV_KINDS',
    'ENV_RUN_STEPS',
    'EnvironmentRemovalBody',
    'EnvironmentRemovalsRecorded',
    'GitHubApp',
    'GitHubAppConnected',
    'GitHubAppImportVerified',
    'GitHubAppInstallOpened',
    'GitHubConnectionAssigned',
    'GitHubConnectionRemoved',
    'GitHubConnectionTokenBody',
    'GitHubInstallationBody',
    'GitHubInstallationSettingsOpened',
    'GitHubManifestCreated',
    'GitHubManifestStarted',
    'GitHubRepositoryAccessChanged',
    'GitHubRepositorySnapshotRecorded',
    'GitHubSecretStore',
    'GitHubTokenConnectionAdded',
    'GitHubTokenConnectionReplaced',
    'HTMLResponse',
    'HTTPException',
    'Path',
    'ProjectAdded',
    'ProjectEnvironmentBody',
    'ProjectEnvironmentSaved',
    'ProjectEnvironmentScanned',
    'ProjectPauseBody',
    'ProjectPauseChanged',
    'ProjectPreflightBody',
    'ProjectRemoved',
    'ProjectRepositoriesBody',
    'ProjectRepositoriesSaved',
    'ProjectServicesBody',
    'ProjectServicesChanged',
    'RedirectResponse',
    'Request',
    'STACK_TOOLS',
    'SimpleNamespace',
    'add_epic_repository_mappings',
    'app_metadata',
    'collect_requested_project_repositories',
    'commit_project_repository_settings',
    'dataclass',
    'environment_reports',
    'first_project_repository_branch_error',
    'html',
    'httpx',
    'include_default_project_repository',
    'install',
    'inventory',
    'json',
    'licensing',
    'load_project_repository_save_context',
    'missing_tools',
    'normalize_services',
    'parse_env_items',
    'partial',
    'project_epic_repository_mappings',
    'publish_for',
    'quote',
    're',
    'system_check',
    'system_check_api',
    'toolchain_gaps',
    'tooling',
    'tools_for_stack',
    'urlsplit',
    'validate_project_repository_save',
    'verify_token',
    'work_limits',
    'yaml',
]



def load_project_repository_save_context(key: str, actor: str, ctx: Any, jira_project_epics: Callable,
                                        github_catalog: Callable, github_connection_list: Callable
                                        ) -> tuple[dict[str, dict[str, Any]], dict[str, Any], set[str]]:
    """Load Jira epics, the connected GitHub catalog, and gateways for one repository save."""
    epics = jira_project_epics(key)
    available_repositories = github_catalog(actor, "project_repository_save")
    available = {str(item["full_name"]).lower(): item for item in available_repositories}
    gateways = {conn.id: conn.gateway for conn in github_connection_list(ctx)}
    epic_keys = {item["key"] for item in epics}
    return available, gateways, epic_keys


def collect_requested_project_repositories(body: ProjectRepositoriesBody, available: dict[str, dict[str, Any]],
                                           default_base: str
                                           ) -> tuple[dict[str, dict[str, Any]], dict[str, str], str]:
    """Normalize requested repository entries and return the first request validation refusal."""
    chosen: dict[str, str] = {}
    repos: dict[str, dict[str, Any]] = {}
    error = ""
    for item in body.repositories:
        name = item.repo.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or name.lower() not in available:
            error = f"repository {name} is not visible to any connected GitHub account"
            break
        if item.connection:
            if item.connection not in available[name.lower()]["connections"]:
                error = f"GitHub connection {item.connection} cannot reach {name}"
                break
            chosen[name.lower()] = item.connection
        if name.lower() in repos:
            error = f"repository {name} is listed more than once"
            break
        base = item.base.strip()
        effective_base = base or default_base
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", effective_base) or ".." in effective_base:
            error = f"invalid base branch for {name}"
            break
        if item.stack and item.stack not in STACK_TOOLS:
            error = f"unsupported stack for {name}"
            break
        repos[name.lower()] = {"repo": name, "base": base,
                               "checks": [check.strip() for check in item.checks if check.strip()],
                               "stack": item.stack.strip(),
                               **({"containers": item.containers} if item.containers is not None else {})}
    return repos, chosen, error


def project_epic_repository_mappings(body: ProjectRepositoriesBody) -> dict[str, str]:
    """Trim Jira epic keys and repository names, omitting mappings whose repository name is blank."""
    return {str(epic).strip(): str(name).strip() for epic, name in body.epic_repositories.items()
            if str(name).strip()}


def add_epic_repository_mappings(key: str, mappings: dict[str, str], epic_keys: set[str],
                                 available: dict[str, dict[str, Any]],
                                 repos: dict[str, dict[str, Any]]) -> str:
    """Validate epic targets and add visible mapped repositories that were not explicitly requested."""
    for epic, name in mappings.items():
        if epic not in epic_keys:
            return f"{epic} is not an epic in Jira project {key}"
        if name.lower() not in available:
            return f"repository {name} is not visible to any connected GitHub account"
        if name.lower() not in repos:
            repos[name.lower()] = {"repo": name, "base": "", "checks": [], "stack": ""}
    return ""


def include_default_project_repository(project: Any, repos: dict[str, dict[str, Any]]) -> None:
    """Add the project's primary repository when the request and epic mappings did not include it."""
    if project.repo.lower() not in repos:
        repos[project.repo.lower()] = {"repo": project.repo, "base": "", "checks": [], "stack": ""}


def first_project_repository_branch_error(project: Any, repos: dict[str, dict[str, Any]],
                                          chosen: dict[str, str], available: dict[str, dict[str, Any]],
                                          gateways: dict[str, Any]) -> str:
    """Return the first missing connection or base branch across the selected repository set."""
    for item in repos.values():
        lowered = item["repo"].lower()
        connection = chosen.get(lowered) or (available.get(lowered) or {}).get("connection", "")
        if not connection or connection not in gateways:
            return f"no GitHub connection can reach {item['repo']}"
        branch = item["base"] or project.base
        if not gateways[connection].branch_exists(item["repo"].split("/", 1)[-1], branch):
            return f"base branch {branch} is missing from {item['repo']}"
    return ""


def validate_project_repository_save(body: ProjectRepositoriesBody, key: str, project: Any,
                                     epic_keys: set[str], available: dict[str, dict[str, Any]],
                                     gateways: dict[str, Any]
                                     ) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, str], str]:
    """Build the complete repository set and return its first request, epic, or branch refusal."""
    repos, chosen, error = collect_requested_project_repositories(body, available, project.base)
    mappings = project_epic_repository_mappings(body)
    if not error:
        error = add_epic_repository_mappings(key, mappings, epic_keys, available, repos)
    if not error:
        include_default_project_repository(project, repos)
        error = first_project_repository_branch_error(project, repos, chosen, available, gateways)
    return repos, chosen, mappings, error


def commit_project_repository_settings(ctx: Any, config_path: Path, key: str,
                                       repos: dict[str, dict[str, Any]], mappings: dict[str, str],
                                       control_gateway: Callable
                                       ) -> tuple[list[dict[str, Any]], str | None]:
    """Write the normalized repository settings to the control repository through GitHub."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    normalized = sorted(repos.values(), key=lambda item: item["repo"].lower())
    config["repositories"] = normalized
    config["epic_repositories"] = mappings
    content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    control_name = Path(ctx.settings.control_repo).name
    github = control_gateway(ctx)
    branch = github.default_branch(control_name)
    commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                               f"projects: update repositories for {key} via AI Flow console")
    return normalized, commit


@dataclass(frozen=True)
class _ProjectRouteRuntime:
    ctx: Any
    audit: Callable
    console_user: Callable
    fenced: Callable
    node_auth: Callable
    optional_console_user: Callable
    require_capacity: Callable
    log: Any
    project_create_body: Any
    required_project_statuses: list[str]
    declare_project_services: Callable
    github_connection_list: Callable
    github_connections: Callable
    github_control_gateway: Callable
    github_gateway: Callable


def _project_key_from_input(value: str, site: str) -> str:
    """Accept a Jira project key or a URL for the selected connected site."""
    raw = value.strip()
    if raw.startswith(("http://", "https://")):
        parsed = urlsplit(raw)
        selected = urlsplit(site)
        if not parsed.hostname or parsed.hostname.lower() != (selected.hostname or "").lower():
            raise HTTPException(422, "Jira project URL must belong to the selected connected site")
        parts = [part for part in parsed.path.split("/") if part]
        candidate = ""
        for index, part in enumerate(parts[:-1]):
            if part in {"projects", "browse"}:
                candidate = parts[index + 1]
                break
        if not candidate:
            raise HTTPException(422, "Jira project URL does not contain a project key")
        raw = candidate.split("-", 1)[0]
    key = raw.upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key):
        raise HTTPException(422, "Jira project must be a project key or project URL")
    return key

def _project_audit(runtime, action: str, key: str, actor: str, accepted: bool, result: str,
                  payload: dict[str, Any] | None = None) -> None:
    runtime.audit("aiflow-control", action, key, "", None, actor, accepted, result,
          payload or {"project": key})

def _refresh_projects(runtime) -> bool:
    if not runtime.ctx.refresh_repo:
        return True
    if runtime.ctx.refresh_repo:
        try:
            runtime.ctx.refresh_repo()
        except Exception as exc:  # RepoCache will retry its pull on a later refresh
            runtime.log.warning("project control repo refresh failed (%s)", type(exc).__name__)
            return False
    return True

def _github_installation_settings(account_login: str, account_type: str, installation_id: int) -> str:
    if account_type.lower() == "organization":
        return (f"https://github.com/organizations/{quote(account_login, safe='-')}/settings/"
                f"installations/{installation_id}")
    return f"https://github.com/settings/installations/{installation_id}"

def _github_secret_store(runtime) -> GitHubSecretStore:
    return GitHubSecretStore(runtime.ctx.settings.github_app_dir)

def _github_install_url(slug: str) -> str:
    return f"https://github.com/apps/{quote(slug, safe='-')}/installations/new"

def _audit_github_repository_changes(runtime, gateway: GitHubApp, repositories: list[dict[str, Any]], actor: str,
                                   source: str) -> None:
    store = _github_secret_store(runtime)
    if not hasattr(gateway, "meta") or not store.active_exists:
        return
    names = sorted({str(item.get("full_name") or "") for item in repositories if item.get("full_name")})
    previous = store.update_repository_snapshot(names)
    if hasattr(gateway, "_refresh_config"):
        gateway._refresh_config()
    if previous is None:
        publish_for(runtime.ctx, GitHubRepositorySnapshotRecorded(actor=actor, count=len(names), source=source))
        return
    added, removed = _repository_snapshot_changes(previous, names)
    if added or removed:
        publish_for(runtime.ctx, GitHubRepositoryAccessChanged(actor=actor, added=added, removed=removed, source=source))
        runtime.ctx.events.emit("github.repositories_changed", account=getattr(gateway, "org", ""),
                        outcome="updated", reason=source, data={"added": added, "removed": removed})

def _repository_snapshot_changes(previous: list[str], current: list[str]) -> tuple[list[str], list[str]]:
    old_by_key = {name.lower(): name for name in previous}
    new_by_key = {name.lower(): name for name in current}
    added = sorted(new_by_key[key] for key in new_by_key.keys() - old_by_key.keys())
    removed = sorted(old_by_key[key] for key in old_by_key.keys() - new_by_key.keys())
    return added, removed

def _connection_summaries(runtime) -> list[dict[str, str]]:
    return [conn.summary() for conn in runtime.github_connection_list(runtime.ctx)]

def _jira_project_epics(runtime, key: str) -> list[dict[str, str]]:
    """Read the current Jira epics for the project instead of relying on the periodic ticket mirror."""
    issues = runtime.ctx.jira.search(f'project = "{key}" AND issuetype = Epic ORDER BY key')
    out = []
    for issue in issues:
        fields = issue.get("fields") or {}
        issue_type = (fields.get("issuetype") or {}).get("name") or ""
        if issue_type and issue_type.lower() != "epic":
            continue
        issue_key = str(issue.get("key") or "")
        if issue_key:
            out.append({"key": issue_key, "summary": str(fields.get("summary") or issue_key),
                        "status": str((fields.get("status") or {}).get("name") or "")})
    return sorted(out, key=lambda item: (int(re.search(r"(\d+)$", item["key"]).group(1))
                                         if re.search(r"(\d+)$", item["key"]) else 0, item["key"]))

def _github_public_base(runtime) -> str:
    value = str(runtime.ctx.settings.public_url or "").rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise HTTPException(503, "set AIFLOW4_PUBLIC_URL to the HTTPS URL owners use to open AI Flow")
    return value

def _connection_credential_view(conn: Connection, meta: dict[str, Any]) -> dict[str, Any]:
    view = {"warning": "", "app": None, "settings_url": ""}
    if conn.kind == "token":
        view["warning"] = ("An access token reaches more than a GitHub App, has no bot identity and cannot "
                           "be limited per run: it stays in Command Center and is never given to a node. "
                           "Prefer a GitHub App.")
    else:
        view["app"] = {"id": str(meta.get("id") or ""), "name": str(meta.get("name") or "GitHub App"),
                       "slug": str(meta.get("slug") or "")}
        installation = meta.get("installation_id") or getattr(conn.gateway, "_inst", None)
        if installation and conn.account:
            view["settings_url"] = _github_installation_settings(conn.account, conn.account_type or "Organization",
                                                                int(installation))
    return view

def _connection_view(runtime, conn: Connection) -> dict[str, Any]:
    """One row of the Integrations list: account, credential type, status and reachable repositories."""
    meta = getattr(conn.gateway, "meta", {}) or {}
    view: dict[str, Any] = {**conn.summary(), "status": "connected", "repositories": [],
                            **_connection_credential_view(conn, meta)}
    try:
        view["repositories"] = sorted(
            str(r.get("full_name")) for r in conn.gateway.installation_repositories()
            if str(r.get("full_name") or "").split("/", 1)[0].lower() == conn.account.lower())
    except Exception as exc:  # provider text may carry credentials; show only a generic status
        runtime.log.warning("GitHub connection %s status failed (%s)", conn.id, type(exc).__name__)
        view["status"] = "error"
    return view

def _verified_github_account(record: dict[str, Any], installation: dict[str, Any]) -> tuple[str, str]:
    account = installation.get("account") or {}
    account_login = str(account.get("login") or "")
    account_type = str(account.get("type") or "")
    if not account_login:
        raise ValueError("GitHub installation account is missing")
    expected = str(record.get("expected_owner") or "")
    if expected and account_login.lower() != expected.lower():
        raise ValueError("GitHub installation account does not match the selected organization")
    if installation.get("suspended_at"):
        raise ValueError("GitHub App installation is suspended")
    return account_login, account_type

def _verified_github_installation(runtime, store: GitHubSecretStore, pending_id: str,
                                  installation_id: int) -> tuple[dict[str, Any], dict[str, Any], str, str, str]:
    record, meta, private_key = store.read_pending(pending_id)
    if not private_key or record.get("phase") not in {"manifest_setup", "import"}:
        raise ValueError("GitHub App setup is incomplete")
    gateway = _pending_gateway(runtime, store, pending_id, meta)
    system_check_api.validate_github_app_permissions(gateway.app_details())
    installation = gateway.installation_details(installation_id)
    if int(installation.get("id") or 0) != installation_id:
        raise ValueError("GitHub installation could not be verified")
    account_login, account_type = _verified_github_account(record, installation)
    return record, meta, private_key, account_login, account_type

def _complete_github_connection(runtime, store: GitHubSecretStore, pending_id: str, installation_id: int,
                               actor: str, source: str) -> dict[str, Any]:
    record, meta, private_key, account_login, account_type = _verified_github_installation(
        runtime, store, pending_id, installation_id)

    # Validate the installation token and selected repository list before activating the key.
    active_meta = app_metadata(meta, account_login=account_login, account_type=account_type, installation_id=installation_id)
    store.update_pending(pending_id, record, active_meta, private_key)
    gateway = _pending_gateway(runtime, store, pending_id, active_meta)
    repositories = gateway.installation_repositories()
    active_meta["repository_names"] = sorted({str(item["full_name"]) for item in repositories
                                              if item.get("full_name")})
    target = str(record.get("connection") or "default")
    if target == "default" or (target == "new" and not store.active_exists):
        connection_id = DEFAULT_ID
        store.save_active(active_meta, private_key)
        runtime.ctx.github = GitHubApp(runtime.ctx.settings.github_app_dir, account_login)
        runtime.ctx.settings.github_org = account_login
    else:
        connection_id = runtime.github_connections(runtime.ctx).save_app(None if target == "new" else target, "",
                                                         active_meta, private_key)
    store.delete_pending(pending_id)
    publish_for(runtime.ctx, GitHubAppConnected(actor=actor, app_id=str(active_meta.get("id") or ""),
                                        installation_id=installation_id, account=account_login,
                                        repositories=len(repositories), source=source, connection=connection_id))
    runtime.ctx.events.emit("github.connected", account=account_login, outcome="connected", reason=source)
    return {"account": account_login, "account_type": account_type, "installation_id": installation_id,
            "connection": connection_id,
            "repositories": len(repositories), "settings_url": _github_installation_settings(
                account_login, account_type, installation_id)}

def _repository_settings_payload(project) -> list[dict[str, Any]]:
    configured = {str(item.get("repo") or "").lower(): item for item in project.repositories
                  if isinstance(item, dict) and item.get("repo")}
    names = project.configured_repositories
    return [{"repo": name, "base": str(configured.get(name.lower(), {}).get("base") or ""),
             "checks": list(configured.get(name.lower(), {}).get("checks") or []),
             "stack": str(configured.get(name.lower(), {}).get("stack") or ""),
             "containers": configured.get(name.lower(), {}).get("containers"),
             "effective": project.settings_for_repo(name)} for name in names]

def _pending_gateway(runtime, store: GitHubSecretStore, pending_id: str, meta: dict[str, Any]) -> GitHubApp:
    directory = store._pending_dir(pending_id)
    return GitHubApp(str(directory), str(meta.get("account_login") or runtime.ctx.settings.github_org))

def _target_connection(runtime, value: Any) -> str:
    """Where a GitHub App being connected goes: the default one, a new connection, or an existing id."""
    text = str(value or "default")
    if text in ("default", "new") or (text != DEFAULT_ID and runtime.github_connections(runtime.ctx).meta(text)):
        return text
    raise HTTPException(422, "unknown GitHub connection")

def _find_connection(runtime, connection_id: str) -> Connection:
    conn = next((c for c in runtime.github_connection_list(runtime.ctx) if c.id == connection_id), None)
    if conn is None:
        raise HTTPException(404, "unknown GitHub connection")
    return conn

def _github_catalog(runtime, actor: str, source: str) -> list[dict[str, Any]]:
    """Repositories of every connection, each with the connections that reach it and the one it uses."""
    connections = runtime.github_connection_list(runtime.ctx)
    assigned = runtime.github_connections(runtime.ctx).assignments()
    repos: dict[str, dict[str, Any]] = {}
    failures: list[Exception] = []
    for conn in connections:
        try:
            rows = conn.gateway.installation_repositories()
            if conn.id == DEFAULT_ID:
                _audit_github_repository_changes(runtime, conn.gateway, rows, actor, source)
        except Exception as exc:  # one broken connection must not hide the others
            runtime.log.warning("GitHub connection %s repositories failed (%s)", conn.id, type(exc).__name__)
            failures.append(exc)
            continue
        for item in rows:
            name = str(item.get("full_name") or "")
            if name.split("/", 1)[0].lower() != conn.account.lower():
                continue
            repos.setdefault(name.lower(), {**item, "connections": []})["connections"].append(conn.id)
    if connections and len(failures) == len(connections):
        raise failures[0]
    for key, item in repos.items():
        assigned_connection = assigned.get(key, "")
        item["assigned_connection"] = assigned_connection
        item["connection"] = assigned_connection if assigned_connection in item["connections"] else (
            "" if assigned_connection else item["connections"][0])
    return sorted(repos.values(), key=lambda row: str(row["full_name"]).lower())

def _ticket_epic(runtime, ticket: dict[str, Any]) -> str:
    """Resolve a ticket's parent chain to its Jira epic key."""
    if ticket.get("type") == "Epic":
        return str(ticket.get("key") or "")
    parent = str(ticket.get("parent_key") or "")
    seen: set[str] = set()
    while parent and parent not in seen:
        seen.add(parent)
        ancestor = ticket_repository_of(runtime.ctx).ticket_ancestor(parent)
        if not ancestor:
            return parent
        if ancestor["type"] == "Epic":
            return parent
        parent = str(ancestor.get("parent_key") or "")
    return ""

def _jira_site(runtime) -> str:
    return str(getattr(runtime.ctx.jira, "site", "") or runtime.ctx.settings.jira_site).rstrip("/")

def _validate_project_preflight_input(runtime, body: ProjectPreflightBody) -> tuple[str, str, str]:
    if not runtime.ctx.jira:
        raise HTTPException(503, "Jira is not connected")
    if not runtime.github_connection_list(runtime.ctx):
        raise HTTPException(503, "GitHub is not connected")
    site = _jira_site(runtime)
    if body.site.rstrip("/") != site:
        raise HTTPException(422, "selected Jira site is not connected")
    key = _project_key_from_input(body.project, site)
    if body.stack and body.stack not in STACK_TOOLS:
        raise HTTPException(422, "unsupported project stack")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", body.base) or ".." in body.base:
        raise HTTPException(422, "invalid base branch")
    org, slash, repo_name = body.repo.partition("/")
    if not slash or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo_name) or not re.fullmatch(r"[A-Za-z0-9_.-]+", org):
        raise HTTPException(422, "select a repository accessible to a connected GitHub account")
    return site, key, repo_name

def _check_project_preflight_providers(runtime, body: ProjectPreflightBody, key: str, repo_name: str,
                                       actor: str) -> tuple[list[str], bool]:
    try:
        jira_statuses = runtime.ctx.jira.project_statuses(key)
        catalog = _github_catalog(runtime, actor, "project_preflight")
        entry = next((item for item in catalog if str(item["full_name"]).lower() == body.repo.lower()), None)
        if entry is None or not entry["connection"]:
            raise HTTPException(422, "repository is not accessible to any connected GitHub account")
        github = runtime.github_gateway(runtime.ctx, body.repo)
        branch_exists = bool(github.branch_exists(repo_name, body.base))
    except HTTPException:
        raise
    except Exception as exc:  # provider details can contain request metadata; keep them out of the UI
        runtime.log.warning("project preflight failed (%s)", type(exc).__name__)
        raise HTTPException(502, "could not complete Jira and GitHub project checks") from exc
    return jira_statuses, branch_exists

def _project_node_readiness(runtime, body: ProjectPreflightBody, jira_statuses: list[str]
                            ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    missing_statuses = [status for status in runtime.required_project_statuses if status not in jira_statuses]
    repo = runtime.ctx.repo()
    node_rows = {row["name"]: row for row in runtime.ctx.db.all("SELECT name,status,tools FROM nodes")}
    node_names = sorted(set(repo.nodes) | set(node_rows))
    required_tools = tools_for_stack(body.stack)
    if body.containers and CONTAINER_RUNTIME_TOOL not in required_tools:
        required_tools.append(CONTAINER_RUNTIME_TOOL)
    node_results = []
    for name in node_names:
        row = node_rows.get(name) or {}
        tools = row.get("tools") or {}
        gaps = toolchain_gaps(tools, required_tools, repo.versions, [body.stack] if body.stack else (),
                              require_pins=bool(body.stack))
        absent = missing_tools(tools, required_tools)
        gaps = list(dict.fromkeys(absent + gaps))
        node_results.append({"name": name, "status": row.get("status", "unknown"),
                             "ready": not gaps, "missing_tools": gaps})
    return missing_statuses, node_results, required_tools

def _project_preflight(runtime, body: ProjectPreflightBody, actor: str = "owner") -> dict[str, Any]:
    site, key, repo_name = _validate_project_preflight_input(runtime, body)
    jira_statuses, branch_exists = _check_project_preflight_providers(runtime, body, key, repo_name, actor)
    missing_statuses, node_results, required_tools = _project_node_readiness(runtime, body, jira_statuses)
    return {"project": key, "site": site, "missing_statuses": missing_statuses,
            "branch_exists": branch_exists, "nodes": node_results, "required_tools": required_tools,
            "ok": not missing_statuses and branch_exists}

def _github_status_account(ctx, meta: dict[str, Any], gateway: GitHubApp) -> tuple[str, str]:
    account = str(meta.get("account_login") or getattr(gateway, "org", "") or ctx.settings.github_org)
    account_type = str(meta.get("account_type") or "Organization")
    return account, account_type

def _decode_github_import(raw: bytearray) -> tuple[dict[str, Any] | None, str]:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None, "invalid App import request"
    if not isinstance(payload, dict):
        return None, "invalid App import request"
    return payload, ""

def _github_private_key_is_valid(pem: Any) -> bool:
    return (isinstance(pem, str) and 100 <= len(pem) <= 20_000 and "\x00" not in pem
            and pem.startswith("-----BEGIN ") and "PRIVATE KEY-----" in pem
            and (pem.rstrip().endswith("-----END PRIVATE KEY-----")
                 or pem.rstrip().endswith("-----END RSA PRIVATE KEY-----")))

def _project_environment_content(config: dict[str, Any], items: list[Any]) -> str:
    config["environment"] = [{key: value for key, value in (
        ("name", item.name), ("kind", item.kind), ("value", item.value), ("runs", list(item.runs)),
        ("description", item.description)) if value not in ("", None)} for item in items]
    if not items:
        config.pop("environment")
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True)

def _project_environment_is_invalid(problems: list[str], items: list[Any], submitted: list[Any]) -> bool:
    return bool(problems or len(items) != len(submitted))

def _project_environment_error(key: str, problems: list[str], submitted: list[dict[str, Any]]
                               ) -> tuple[str, dict[str, Any]]:
    message = "; ".join(problems) or "every environment item needs a name and a kind"
    names = [str(item.get("name", ""))[:64] for item in submitted]
    return message, {"project": key, "names": names}

def _unknown_project_services(services: list[str]) -> list[str]:
    return [service for service in services if service.strip() and not normalize_services(service)]

def _project_preflight_failure(body: Any, result: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    reasons = []
    if result["missing_statuses"]:
        reasons.append("missing Jira statuses: " + ", ".join(result["missing_statuses"]))
    if not result["branch_exists"]:
        reasons.append(f"base branch {body.base} is missing from {body.repo}")
    detail = {"message": "; ".join(reasons) or "project checks failed",
              "missing_statuses": result["missing_statuses"],
              "branch_exists": result["branch_exists"], "nodes": result["nodes"]}
    return reasons, detail

def _new_project_config(body: Any, key: str) -> dict[str, Any]:
    checks = [check.strip() for check in body.checks if check.strip()]
    return {"key": key, "repo": body.repo, "base": body.base, "stack": body.stack,
            "services": normalize_services(body.services), "checks": checks, "containers": body.containers,
            "merge": {"method": body.merge_method},
            "limits": {"parallel": body.parallel_limit, "work_minutes": dict(work_limits.DEFAULTS)},
            "priority": body.priority, "paused": False}

def _commit_new_project(runtime, ctx, control_name: str, key: str, actor: str,
                        content: str, config: dict[str, Any]) -> str | None:
    try:
        github = runtime.github_control_gateway(ctx)
        branch = github.default_branch(control_name)
        return github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                  f"projects: add {key} via AI Flow console")
    except HTTPException:
        raise
    except Exception as exc:
        runtime.log.warning("project add commit failed (%s)", type(exc).__name__)
        _project_audit(runtime, "add_project", key, actor, False, "control repo commit failed", config)
        raise HTTPException(502, "could not save the project to aiflow-control") from exc

def _convert_github_manifest(code: str, expected_owner: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]+", code):
        raise ValueError("GitHub manifest response is invalid")
    with httpx.Client(base_url="https://api.github.com", timeout=30,
                      headers={"Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aiflow4"}) as client:
        response = client.post(f"/app-manifests/{quote(code, safe='')}/conversions")
    if response.status_code not in (200, 201):
        raise ValueError("GitHub manifest could not be completed")
    converted = response.json()
    pem = converted.get("pem")
    if (not converted.get("id") or not converted.get("slug") or not isinstance(pem, str)
            or not pem.startswith("-----BEGIN ")):
        raise ValueError("GitHub manifest response is incomplete")
    app_owner = converted.get("owner") or {}
    if (str(app_owner.get("login") or "").lower() != expected_owner.lower()
            or str(app_owner.get("type") or "").lower() != "organization"):
        raise ValueError("GitHub App owner does not match the selected organization")
    return converted

def _delete_failed_manifest_state(store: GitHubSecretStore, state: str) -> str:
    actor = "github-manifest"
    try:
        record, _, _ = store.read_pending(state)
        actor = str(record.get("actor") or actor)
        store.delete_pending(state)
    except Exception:
        pass
    return actor

def _verify_imported_github_app(runtime, store: GitHubSecretStore, pending_id: str, app_id: int,
                               pem: str, record: dict[str, Any]) -> dict[str, Any]:
    _, meta, _ = store.read_pending(pending_id)
    gateway = _pending_gateway(runtime, store, pending_id, meta)
    details = gateway.app_details()
    if int(details.get("id") or 0) != app_id:
        raise ValueError("GitHub App id did not match")
    system_check_api.validate_github_app_permissions(details)
    if not details.get("slug"):
        raise ValueError("GitHub App slug is missing")
    record["app"] = app_metadata(details)
    store.update_pending(pending_id, record, app_metadata(details), pem)
    return details

def _github_import_result(pending_id: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"pending_id": pending_id,
            "app": {"id": str(details.get("id") or ""), "name": str(details.get("name") or "GitHub App"),
                    "slug": str(details.get("slug") or "")},
            "install_url": _github_install_url(str(details.get("slug") or ""))}

def _project_route_helper_namespace(runtime) -> SimpleNamespace:
    return SimpleNamespace(
        github_secret_store=partial(_github_secret_store, runtime),
        audit_github_repository_changes=partial(_audit_github_repository_changes, runtime),
        github_install_url=_github_install_url,
        github_installation_settings=_github_installation_settings,
        github_public_base=partial(_github_public_base, runtime),
        pending_gateway=partial(_pending_gateway, runtime),
        complete_github_connection=partial(_complete_github_connection, runtime),
        target_connection=partial(_target_connection, runtime),
        connection_view=partial(_connection_view, runtime),
        find_connection=partial(_find_connection, runtime),
        required_project_statuses=runtime.required_project_statuses,
        jira_site=partial(_jira_site, runtime),
        project_key_from_input=_project_key_from_input,
        github_catalog=partial(_github_catalog, runtime),
        connection_summaries=partial(_connection_summaries, runtime),
        project_preflight=partial(_project_preflight, runtime),
        refresh_projects=partial(_refresh_projects, runtime),
        project_audit=partial(_project_audit, runtime),
        ticket_epic=partial(_ticket_epic, runtime),
        jira_project_epics=partial(_jira_project_epics, runtime),
        repository_settings_payload=_repository_settings_payload,
    )


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import ProjectCreateBody, log  # noqa: PLC0415 - api imports this module
    from ...gateway_helpers import declare_project_services, github_connection_list, github_connections, github_control_gateway, github_gateway  # noqa: PLC0415 - api imports this module
    audit = deps.audit
    console_user = deps.console_user
    fenced = deps.fenced
    node_auth = deps.node_auth
    optional_console_user = deps.optional_console_user
    require_capacity = deps.require_capacity
    runtime = _ProjectRouteRuntime(
        ctx=ctx, audit=audit, console_user=console_user, fenced=fenced, node_auth=node_auth,
        optional_console_user=optional_console_user, require_capacity=require_capacity,
        log=log, project_create_body=ProjectCreateBody,
        required_project_statuses=list(system_check.REQUIRED_PROJECT_STATUSES),
        declare_project_services=declare_project_services, github_connection_list=github_connection_list,
        github_connections=github_connections, github_control_gateway=github_control_gateway,
        github_gateway=github_gateway)
    support = _project_route_helper_namespace(runtime)
    from .routes_integrations import install_integrations_routes  # noqa: PLC0415
    from .routes_project_listing import install_project_listing_routes  # noqa: PLC0415
    from .routes_project_repositories import install_project_repository_routes  # noqa: PLC0415
    from .routes_project_environment import install_project_environment_routes  # noqa: PLC0415
    from .routes_environment import install_environment_routes  # noqa: PLC0415
    from .routes_project_lifecycle import install_project_lifecycle_routes  # noqa: PLC0415
    integrations = install_integrations_routes(app, ctx, deps, runtime, support)
    listing = install_project_listing_routes(app, ctx, deps, runtime, support)
    repositories = install_project_repository_routes(app, ctx, deps, runtime, support)
    project_environment = install_project_environment_routes(app, ctx, deps, runtime, support)
    environment = install_environment_routes(app, ctx, deps, runtime, support)
    lifecycle = install_project_lifecycle_routes(app, ctx, deps, runtime, support)
    return SimpleNamespace(**vars(support), **vars(integrations), **vars(listing), **vars(repositories),
                           **vars(project_environment), **vars(environment), **vars(lifecycle))
