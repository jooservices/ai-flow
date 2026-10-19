"""Project registry: one TOML file for the general settings and one per Jira project.

    JOOservices/config/aiflow.toml          [defaults] shared by every project, [machines.<name>] workers
    JOOservices/config/aiflow/<name>.toml   [project] jira_key, repo and the project's own settings

Every file of the directory is loaded. A project value beats `[defaults]`, which beats the legacy `.env` value
(so a machine without TOML files keeps working). Secrets stay in `.env`.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .config import ConfigError

SETTINGS = {"base": str, "hotfix_base": str, "developer": str, "reviewer": str, "qa": str, "require_human": bool,
            "auto_merge": bool, "ci_required": bool, "merge_method": str, "max_parallel": int}
IDENTITY = {"name": str, "jira_key": str, "repo": str, "company": str}
MERGE_METHODS = ("merge", "squash", "rebase")


@dataclass(frozen=True)
class Project:
    jira_key: str
    repo: str
    name: str = ""
    company: str = "JOOservices"
    base: str = "develop"
    hotfix_base: str = "master"
    developer: str = ""
    reviewer: str = ""
    qa: str = ""
    require_human: bool = False
    auto_merge: bool = True
    ci_required: bool = False
    merge_method: str = "merge"
    max_parallel: int = 0  # tickets of the project that may run at the same time, 0 = no limit

    def role(self, role: str) -> str:
        return {"developer": self.developer, "reviewer": self.reviewer, "qa": self.qa}.get(role, "")


@dataclass(frozen=True)
class Registry:
    projects: dict[str, Project]
    workers: dict[str, int]  # machine -> number of workers; empty when the machine is not listed
    from_files: bool

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
    for machine, table in general.get("machines", {}).items():
        count = table.get("workers") if isinstance(table, dict) else None
        if not isinstance(count, int) or isinstance(count, bool) or count < 1 or set(table) != {"workers"}:
            raise ConfigError(f"{general_file} [machines.{machine}]: needs `workers` as a whole number of at least 1 and nothing else.")
        workers[machine] = count
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
        if project.merge_method not in MERGE_METHODS:
            raise ConfigError(f"{path}: merge_method must be one of {', '.join(MERGE_METHODS)}.")
        if project.jira_key in projects:
            raise ConfigError(f"Jira project {project.jira_key} is declared in {owners[project.jira_key].name} and in {path.name}.")
        projects[project.jira_key], owners[project.jira_key] = project, path
    if not projects:
        raise ConfigError(f"No project file in {config_dir / 'aiflow'}.")
    return Registry(projects, workers, from_files=True)

