"""Project registry: one TOML file for the general settings and one per Jira project.

    JOOservices/config/aiflow.toml          [defaults] shared by every project, [machines.<name>] workers, [runtime] supervisor tuning
    JOOservices/config/aiflow/<name>.toml   [project] jira_key, repo and the project's own settings

Every file of the directory is loaded. A project value beats `[defaults]`, which beats the legacy `.env` value
(so a machine without TOML files keeps working). `[runtime]` beats the legacy `.env` value of the same setting.
Secrets and the identity of the machine (AIFLOW_MACHINE, Redis connection) stay in `.env`.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import ConfigError

SETTINGS = {"base": str, "hotfix_base": str, "developer": str, "reviewer": str, "qa": str, "require_human": bool,
            "human_on_deployment": bool, "auto_merge": bool, "ci_required": bool, "merge_method": str, "max_parallel": int,
            "loop_limit": int,
            "highest_developer": str, "release_check": str, "tag_prefix": str, "deploy_command": str,
            "deploy_timeout_minutes": int, "deploy_on": str, "workers": int}
IDENTITY = {"name": str, "jira_key": str, "repo": str, "company": str, "database": str}
MERGE_METHODS = ("merge", "squash", "rebase")
DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
# [runtime] of aiflow.toml: setting -> (type, the AIFLOW_* name it replaces). A list is written as a TOML array.
RUNTIME = {
    "poll_seconds": (int, "AIFLOW_POLL_SECONDS"), "heartbeat_seconds": (int, "AIFLOW_HEARTBEAT_SECONDS"),
    "lease_seconds": (int, "AIFLOW_LEASE_SECONDS"), "orphan_grace_seconds": (int, "AIFLOW_ORPHAN_GRACE_SECONDS"),
    "idle_minutes": (int, "AIFLOW_IDLE_MINUTES"), "max_minutes": (int, "AIFLOW_MAX_MINUTES"),
    "worker_max_minutes": (int, "AIFLOW_WORKER_MAX_MINUTES"), "max_attempts": (int, "AIFLOW_MAX_ATTEMPTS"),
    "quota_cooldown_minutes": (int, "AIFLOW_QUOTA_COOLDOWN_MINUTES"), "quota_block_minutes": (int, "AIFLOW_QUOTA_BLOCK_MINUTES"),
    "recheck_seconds": (int, "AIFLOW_RECHECK_SECONDS"), "probe_hours": (int, "AIFLOW_PROBE_HOURS"),
    "wait_note_minutes": (int, "AIFLOW_WAIT_NOTE_MINUTES"), "heartbeat_stale_seconds": (int, "AIFLOW_HEARTBEAT_STALE_SECONDS"),
    "ci_wait_minutes": (int, "AIFLOW_CI_WAIT_MINUTES"), "feedback_settle_minutes": (int, "AIFLOW_FEEDBACK_SETTLE_MINUTES"),
    "gate_poll_seconds": (int, "AIFLOW_GATE_POLL_SECONDS"), "max_cycles": (int, "AIFLOW_MAX_CYCLES"),
    "loop_limit": (int, "AIFLOW_MAX_CYCLES"),
    "agent_order": (list, "AIFLOW_AGENT_ORDER"), "agent_models": (list, "AIFLOW_AGENT_MODELS"), "explicit_only_ais": (list, "AIFLOW_EXPLICIT_ONLY_AIS"),
    "ignored_authors": (list, "AIFLOW_IGNORED_AUTHORS"), "opencode_model": (str, "AIFLOW_OPENCODE_MODEL"),
    "health_ticket": (str, "AIFLOW_HEALTH_TICKET"), "workspace_name": (str, "AIFLOW_WORKSPACE_NAME"),
    "workspace_field": (str, "AIFLOW_WORKSPACE_FIELD"), "blocked_reason_field": (str, "AIFLOW_BLOCKED_REASON_FIELD"),
    "require_human_field": (str, "AIFLOW_REQUIRE_HUMAN_FIELD"), "model_field": (str, "AIFLOW_MODEL_FIELD"),
    "type_field": (str, "AIFLOW_TYPE_FIELD"), "version_field": (str, "AIFLOW_VERSION_FIELD"),
    "redis_prefix": (str, "AIFLOW_REDIS_PREFIX"),
    "memory_url": (str, "AIFLOW_MEMORY_URL"), "obs_url": (str, "AIFLOW_OBS_URL"), "obs_org": (str, "AIFLOW_OBS_ORG"), "obs_stream": (str, "AIFLOW_OBS_STREAM"),
}
AI_LIMIT_NAMES = ("claude", "codex", "grok", "opencode")


@dataclass(frozen=True)
class Project:
    jira_key: str
    repo: str
    name: str = ""
    company: str = "JOOservices"
    database: str = ""  # empty disables per-ticket database provisioning
    base: str = "develop"
    hotfix_base: str = "master"
    developer: str = ""
    reviewer: str = ""
    qa: str = ""
    require_human: bool = False
    human_on_deployment: bool = True
    auto_merge: bool = True
    ci_required: bool = False
    merge_method: str = "merge"
    max_parallel: int = 0  # tickets of the project that may run at the same time, 0 = no limit
    release_check: str = ""  # command that runs the full test suite (and e2e) of a release, written by the project
    tag_prefix: str = "v"
    highest_developer: str = "Claude"  # developer of the tickets with priority Highest (the Agent (AI) / AI Model fields still win)
    deploy_command: str = ""
    deploy_timeout_minutes: int = 30
    deploy_on: str = "base"  # "all" also runs after hotfix and release merges
    workers: int = 0  # workers of this project (aiflow-work@<KEY>-<n>); worker 1 serves only this project, the others may take other projects' tickets
    loop_limit: int | None = None  # None uses the global supervisor loop limit

    def role(self, role: str) -> str:
        return {"developer": self.developer, "reviewer": self.reviewer, "qa": self.qa}.get(role, "")


@dataclass(frozen=True)
class Registry:
    projects: dict[str, Project]
    workers: dict[str, int]  # machine -> number of workers; empty when the machine is not listed
    from_files: bool
    ai_limits: dict[str, dict[str, int]] = field(default_factory=dict)  # machine -> AI name -> concurrent session limit
    runtime: dict[str, str] = field(default_factory=dict)  # AIFLOW_* name -> value, from [runtime] of aiflow.toml
    max_workers: dict[str, int] = field(default_factory=dict)  # machine -> most workers (shared + all projects) it may run

    def get(self, key: str) -> Project | None:
        return self.projects.get(key)

    def of_issue(self, issue_key: str) -> Project:
        project = self.projects.get(issue_key.rsplit("-", 1)[0])
        if project is None and not self.from_files:
            return next(iter(self.projects.values()))  # legacy .env: one set of values for whatever the machine watches
        if project is None:
            raise ConfigError(f"Issue {issue_key} belongs to a Jira project that no file in config/aiflow/ declares.")
        return project


def _typed(table: dict, allowed: dict[str, type], where: str) -> dict:
    for name, value in table.items():
        if name not in allowed:
            raise ConfigError(f"{where}: unknown setting `{name}` (known: {', '.join(sorted(allowed))}).")
        expected = allowed[name]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ConfigError(f"{where}: `{name}` must be {expected.__name__}, got {value!r}.")
    return table


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as error:
        raise ConfigError(f"{path}: {error}") from error


def _runtime(table: dict, where: str) -> dict[str, str]:
    """The `[runtime]` table as AIFLOW_* values; a list becomes a comma separated string."""
    if "max_cycles" in table and "loop_limit" in table:
        raise ConfigError(f"{where}: set `max_cycles` or `loop_limit`, not both.")
    unknown = sorted(set(table) - set(RUNTIME))
    if unknown:
        raise ConfigError(f"{where}: unknown setting {', '.join(unknown)} (known: {', '.join(sorted(RUNTIME))}).")
    values: dict[str, str] = {}
    for key, value in table.items():
        kind, name = RUNTIME[key]
        if kind is int and (not isinstance(value, int) or isinstance(value, bool)):
            raise ConfigError(f"{where}: `{key}` must be a whole number.")
        if kind is str and not isinstance(value, str):
            raise ConfigError(f"{where}: `{key}` must be text.")
        if kind is list and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
            raise ConfigError(f"{where}: `{key}` must be a list of text values.")
        values[name] = ",".join(value) if kind is list else str(value)
    return values


def _project_settings(table: dict, where: str) -> dict:
    """Accept the legacy setting name as an alias, while refusing two names for one value in a table."""
    table = dict(table)
    if "max_cycles" in table and "loop_limit" in table:
        raise ConfigError(f"{where}: set `max_cycles` or `loop_limit`, not both.")
    if "max_cycles" in table:
        table["loop_limit"] = table.pop("max_cycles")
    return table


def desired_ids(registry: Registry, machine: str) -> list[str]:
    """The worker ids this machine should run: the shared numbers 1..N (any project), then <KEY>-1..<KEY>-n per project."""
    ids = [str(n) for n in range(1, registry.workers.get(machine, 0) + 1)]
    for key in sorted(registry.projects):
        ids += [f"{key}-{n}" for n in range(1, registry.projects[key].workers + 1)]
    return ids


def legacy(keys: tuple[str, ...], base: Project) -> Registry:
    """The registry of a machine without TOML files: every watched Jira project uses the `.env` values."""
    return Registry({key: replace(base, jira_key=key) for key in keys}, {}, from_files=False)


def load(config_dir: Path, fallback: Project) -> Registry | None:
    """The registry of `config_dir`, or None when `aiflow.toml` does not exist (legacy mode).
    `fallback` carries the `.env` values for everything the files leave out."""
    general_file = config_dir / "aiflow.toml"
    if not general_file.is_file():
        return None
    general = _read(general_file)
    for section in general:
        if section not in ("defaults", "machines", "runtime"):
            raise ConfigError(f"{general_file}: unknown section `{section}` (known: defaults, machines, runtime).")
    defaults = replace(fallback, **_typed(_project_settings(general.get("defaults", {}), f"{general_file} [defaults]"),
                                           SETTINGS, f"{general_file} [defaults]"))
    if defaults.loop_limit is not None and defaults.loop_limit < 0:
        raise ConfigError(f"{general_file} [defaults]: `loop_limit` must be a whole number of 0 or more.")
    workers: dict[str, int] = {}
    ai_limits: dict[str, dict[str, int]] = {}
    max_workers: dict[str, int] = {}
    for machine, table in general.get("machines", {}).items():
        if not isinstance(table, dict) or set(table) - {"workers", "ai_limits", "max_workers"}:
            raise ConfigError(f"{general_file} [machines.{machine}]: only `workers`, `max_workers` and `ai_limits` are allowed.")
        count = table.get("workers", 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ConfigError(f"{general_file} [machines.{machine}]: `workers` (the shared workers, any project) must be a whole number of 0 or more.")
        workers[machine] = count
        if "max_workers" in table:
            cap = table["max_workers"]
            if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
                raise ConfigError(f"{general_file} [machines.{machine}]: `max_workers` must be a whole number of at least 1.")
            max_workers[machine] = cap
        limits = table.get("ai_limits", {})
        if not isinstance(limits, dict):
            raise ConfigError(f"{general_file} [machines.{machine}.ai_limits]: must be a table.")
        limits = _typed(limits, {name: int for name in AI_LIMIT_NAMES}, f"{general_file} [machines.{machine}.ai_limits]")
        if any(value < 0 for value in limits.values()):
            raise ConfigError(f"{general_file} [machines.{machine}.ai_limits]: limits must be zero or greater.")
        if limits:
            ai_limits[machine] = dict(limits)
    projects: dict[str, Project] = {}
    owners: dict[str, Path] = {}
    for path in sorted((config_dir / "aiflow").glob("*.toml")):
        data = _read(path)
        if set(data) != {"project"} or not isinstance(data["project"], dict):
            raise ConfigError(f"{path}: needs one [project] table and nothing else.")
        table = _typed(_project_settings(data["project"], f"{path} [project]"), {**IDENTITY, **SETTINGS}, f"{path} [project]")
        for needed in ("jira_key", "repo"):
            if not table.get(needed):
                raise ConfigError(f"{path} [project]: `{needed}` is required.")
        project = replace(defaults, **table)
        if project.loop_limit is not None and project.loop_limit < 0:
            raise ConfigError(f"{path} [project]: `loop_limit` must be a whole number of 0 or more.")
        if project.database and not DATABASE_NAME.fullmatch(project.database):
            raise ConfigError(f"{path}: database must be 2-24 lowercase letters, digits, '-' or '_', starting with a letter.")
        if project.merge_method not in MERGE_METHODS:
            raise ConfigError(f"{path}: merge_method must be one of {', '.join(MERGE_METHODS)}.")
        if project.deploy_timeout_minutes < 1:
            raise ConfigError(f"{path}: deploy_timeout_minutes must be a whole number of at least 1.")
        if project.deploy_on not in ("base", "all"):
            raise ConfigError(f"{path}: deploy_on must be 'base' or 'all'.")
        if project.jira_key in projects:
            raise ConfigError(f"Jira project {project.jira_key} is declared in {owners[project.jira_key].name} and in {path.name}.")
        if project.workers < 0:
            raise ConfigError(f"{path}: workers must be a whole number of 0 or more.")
        projects[project.jira_key], owners[project.jira_key] = project, path
    if not projects:
        raise ConfigError(f"No project file in {config_dir / 'aiflow'}.")
    runtime = _runtime(general.get("runtime", {}), f"{general_file} [runtime]")
    registry = Registry(projects, workers, from_files=True, ai_limits=ai_limits, runtime=runtime, max_workers=max_workers)
    for machine, cap in max_workers.items():
        total = len(desired_ids(registry, machine))
        if total > cap:
            raise ConfigError(f"{general_file} [machines.{machine}]: the workers add up to {total} (shared {workers.get(machine, 0)} + projects "
                              f"{total - workers.get(machine, 0)}) but max_workers is {cap}.")
    return registry

