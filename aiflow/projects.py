"""Project registry: one TOML file for the general settings and one per Jira project.

    JOOservices/config/aiflow.toml          [defaults] shared by every project, [machines.<name>] workers
    JOOservices/config/aiflow/<name>.toml   [project] jira_key, repo and the project's own settings

Every file of the directory is loaded. A project value beats `[defaults]`, which beats the legacy `.env` value
(so a machine without TOML files keeps working). Secrets stay in `.env`.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import ConfigError

SETTINGS = {"base": str, "hotfix_base": str, "developer": str, "reviewer": str, "qa": str, "require_human": bool,
            "human_on_deployment": bool, "auto_merge": bool, "ci_required": bool, "merge_method": str, "max_parallel": int,
            "highest_developer": str, "release_check": str, "tag_prefix": str, "deploy_command": str,
            "deploy_timeout_minutes": int, "deploy_on": str}
IDENTITY = {"name": str, "jira_key": str, "repo": str, "company": str, "database": str}
MERGE_METHODS = ("merge", "squash", "rebase")
DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
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

    def role(self, role: str) -> str:
        return {"developer": self.developer, "reviewer": self.reviewer, "qa": self.qa}.get(role, "")


@dataclass(frozen=True)
class Registry:
    projects: dict[str, Project]
    workers: dict[str, int]  # machine -> number of workers; empty when the machine is not listed
    from_files: bool
    ai_limits: dict[str, dict[str, int]] = field(default_factory=dict)  # machine -> AI name -> concurrent session limit

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
        if section not in ("defaults", "machines"):
            raise ConfigError(f"{general_file}: unknown section `{section}` (known: defaults, machines).")
    defaults = replace(fallback, **_typed(general.get("defaults", {}), SETTINGS, f"{general_file} [defaults]"))
    workers: dict[str, int] = {}
    ai_limits: dict[str, dict[str, int]] = {}
    for machine, table in general.get("machines", {}).items():
        if not isinstance(table, dict) or set(table) - {"workers", "ai_limits"}:
            raise ConfigError(f"{general_file} [machines.{machine}]: only `workers` and `ai_limits` are allowed.")
        count = table.get("workers")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ConfigError(f"{general_file} [machines.{machine}]: needs `workers` as a whole number of at least 1.")
        workers[machine] = count
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
        table = _typed(data["project"], {**IDENTITY, **SETTINGS}, f"{path} [project]")
        for needed in ("jira_key", "repo"):
            if not table.get(needed):
                raise ConfigError(f"{path} [project]: `{needed}` is required.")
        project = replace(defaults, **table)
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
        projects[project.jira_key], owners[project.jira_key] = project, path
    if not projects:
        raise ConfigError(f"No project file in {config_dir / 'aiflow'}.")
    return Registry(projects, workers, from_files=True, ai_limits=ai_limits)

