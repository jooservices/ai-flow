"""Settings for the AI Flow supervisor. Everything comes from the environment or `.env`."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
WORKSPACES_ROOT = Path(os.environ.get("AIFLOW_ROOT", PROJECT_DIR.parents[2])).resolve()


PROFILE_DIR = Path(os.environ.get("AIFLOW_PROFILE_DIR", WORKSPACES_ROOT / "JOOservices" / "config" / "aiflow")).resolve()


class ConfigError(Exception):
    """A worker cannot start because its configuration is wrong (shown to the owner, no traceback)."""


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.removeprefix("export ").strip()] = value.strip().strip("'\"")
    return values


def _int(env: dict[str, str], key: str, default: int) -> int:
    return int(env.get(key, default))


def _list(env: dict[str, str], key: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in env.get(key, default).split(",") if item.strip())


def repo_listed(names: tuple[str, ...], repo: str) -> bool:
    return "*" in names or repo in names


@dataclass(frozen=True)
class Settings:
    root: Path
    projects: tuple[str, ...]
    machine: str
    slot: int
    profile: str
    workers: int
    worker_max_minutes: int
    poll_seconds: int
    heartbeat_seconds: int
    lease_seconds: int
    idle_minutes: int
    max_minutes: int
    orphan_grace_seconds: int
    max_attempts: int
    quota_cooldown_minutes: int
    redis_host: str
    redis_port: int
    redis_user: str
    redis_db: int
    redis_prefix: str
    redis_password: str
    default_repo: str
    default_base: str
    agent_order: tuple[str, ...]
    wait_note_minutes: int
    expected_slots: tuple[str, ...]
    health_ticket: str
    heartbeat_stale_seconds: int
    role_defaults: dict
    auto_merge_repos: tuple[str, ...]
    ci_required_repos: tuple[str, ...]
    ci_wait_minutes: int
    feedback_settle_minutes: int
    gate_poll_seconds: int
    max_cycles: int
    merge_method: str
    ignored_authors: tuple[str, ...]
    workspace_field: str
    blocked_reason_field: str
    require_human_field: str
    model_field: str
    opencode_model: str
    explicit_only: tuple[str, ...]
    quota_block_minutes: int
    recheck_seconds: int
    workspace_name: str

    @property
    def worker_label(self) -> str:
        """`<profile>-<n>` for a profile worker, the bare slot number for a legacy supervisor."""
        return f"{self.profile}-{self.slot}" if self.profile else str(self.slot)

    @property
    def owner(self) -> str:
        return f"{self.machine}:{self.worker_label}:{os.getpid()}"


def parse_worker_id(worker_id: str) -> tuple[str, int]:
    """`B-2` -> (`B`, 2). The profile name may itself contain dashes (`jira-b-2` -> `jira-b`, 2)."""
    profile, _, number = worker_id.rpartition("-")
    if not profile or not number.isdigit() or int(number) < 1:
        raise ConfigError(f"Worker id `{worker_id}` must look like <profile>-<number>, for example B-2.")
    return profile, int(number)


def profile_file(profile: str, directory: Path | None = None) -> Path:
    return (directory or PROFILE_DIR) / f"{profile}.env"


def profile_projects(values: dict[str, str]) -> tuple[str, ...]:
    return tuple(p.strip() for p in values.get("AIFLOW_PROJECTS", "").split(",") if p.strip())


def profile_workers(values: dict[str, str]) -> int:
    return max(1, int(values.get("AIFLOW_WORKERS", 1)))


def all_profiles(directory: Path | None = None) -> dict[str, dict[str, str]]:
    """Every profile file of the directory: name -> its settings."""
    found: dict[str, dict[str, str]] = {}
    base = directory or PROFILE_DIR
    if base.is_dir():
        for path in sorted(base.glob("*.env")):
            found[path.stem] = read_env_file(path)
    return found


def check_profiles(profiles: dict[str, dict[str, str]]) -> None:
    """A Jira project belongs to one profile; every profile watches at least one project."""
    owners: dict[str, str] = {}
    for name, values in profiles.items():
        projects = profile_projects(values)
        if not projects:
            raise ConfigError(f"Profile {name} has no AIFLOW_PROJECTS.")
        for project in projects:
            if project in owners and owners[project] != name:
                raise ConfigError(f"Project {project} is watched by profile {owners[project]} and profile {name}. A project belongs to one profile.")
            owners[project] = name


def expected_workers(merged: dict[str, str], machine: str, directory: Path | None = None) -> tuple[str, ...]:
    """Heartbeats the health check expects: `<machine>:<profile>-<n>` for every profile file, otherwise the
    legacy AIFLOW_EXPECTED_SLOTS list."""
    profiles = all_profiles(directory)
    if profiles:
        return tuple(f"{machine}:{name}-{n}" for name, values in profiles.items() for n in range(1, profile_workers(values) + 1))
    return tuple(r.strip() for r in merged.get("AIFLOW_EXPECTED_SLOTS", "wsl:1,wsl:2").split(",") if r.strip())


def load(env: dict[str, str] | None = None, worker_id: str | None = None, config: Path | None = None,
         profile_dir: Path | None = None) -> Settings:
    """Settings of one worker. `worker_id` (`B-2`) or `config` (a profile file) picks the profile; without either
    the worker is a legacy supervisor and AIFLOW_SLOT / AIFLOW_PROJECTS of the common files apply."""
    if env is None:  # an explicit environment (tests) must not pick up the machine's .env files
        merged = dict(read_env_file(WORKSPACES_ROOT / ".env"))
        merged.update(read_env_file(WORKSPACES_ROOT / "JOOservices" / ".env"))
        merged.update(read_env_file(PROJECT_DIR / ".env"))
        shell = dict(os.environ)
    else:
        merged = dict(env)
        shell = {}

    profile = ""
    slot_from_id: int | None = None
    if worker_id:
        profile, slot_from_id = parse_worker_id(worker_id)
    if config is not None or profile:
        path = Path(config) if config is not None else profile_file(profile, profile_dir)
        if not path.is_file():
            raise ConfigError(f"Profile file {path} was not found.")
        values = read_env_file(path)
        merged.update(values)
        profile = profile or values.get("AIFLOW_PROFILE", path.stem)
        directory = profile_dir or path.parent
        profiles = all_profiles(directory)
        profiles[profile] = values
        check_profiles(profiles)
        if slot_from_id is not None and slot_from_id > profile_workers(values):
            raise ConfigError(f"Profile {profile} runs {profile_workers(values)} worker(s); {worker_id} is outside that number.")
    merged.update(shell)
    if slot_from_id is not None:
        merged["AIFLOW_SLOT"] = str(slot_from_id)
    machine = merged.get("AIFLOW_MACHINE", socket.gethostname().lower())
    return Settings(
        root=WORKSPACES_ROOT,
        projects=tuple(p.strip() for p in merged.get("AIFLOW_PROJECTS", "JAIF").split(",") if p.strip()),
        machine=machine,
        slot=_int(merged, "AIFLOW_SLOT", 1),
        profile=profile,
        workers=profile_workers(merged),
        worker_max_minutes=_int(merged, "AIFLOW_WORKER_MAX_MINUTES", 0),
        poll_seconds=_int(merged, "AIFLOW_POLL_SECONDS", 30),
        heartbeat_seconds=_int(merged, "AIFLOW_HEARTBEAT_SECONDS", 20),
        lease_seconds=_int(merged, "AIFLOW_LEASE_SECONDS", 120),
        idle_minutes=_int(merged, "AIFLOW_IDLE_MINUTES", 30),
        max_minutes=_int(merged, "AIFLOW_MAX_MINUTES", 180),
        orphan_grace_seconds=_int(merged, "AIFLOW_ORPHAN_GRACE_SECONDS", 180),
        max_attempts=_int(merged, "AIFLOW_MAX_ATTEMPTS", 3),
        quota_cooldown_minutes=_int(merged, "AIFLOW_QUOTA_COOLDOWN_MINUTES", 30),
        redis_host=merged.get("AIFLOW_REDIS_HOST", "127.0.0.1"),
        redis_port=_int(merged, "AIFLOW_REDIS_PORT", 16382),
        redis_user=merged.get("AIFLOW_REDIS_USER", ""),
        redis_db=_int(merged, "AIFLOW_REDIS_DB", int(merged.get("REDIS_DB", 5))),
        redis_prefix=merged.get("AIFLOW_REDIS_PREFIX", "aiworkflow:"),
        redis_password=merged.get(merged.get("AIFLOW_REDIS_PASSWORD_VAR", "WORKSPACES_REDIS_PASSWORD"), ""),
        default_repo=merged.get("AIFLOW_DEFAULT_REPO", "ai-flow"),
        default_base=merged.get("AIFLOW_DEFAULT_BASE", "develop"),
        agent_order=tuple(a.strip() for a in merged.get("AIFLOW_AGENT_ORDER", "Claude,Codex,OpenCode").split(",") if a.strip()),
        wait_note_minutes=_int(merged, "AIFLOW_WAIT_NOTE_MINUTES", 10),
        expected_slots=expected_workers(merged, machine, profile_dir),
        health_ticket=merged.get("AIFLOW_HEALTH_TICKET", ""),
        heartbeat_stale_seconds=_int(merged, "AIFLOW_HEARTBEAT_STALE_SECONDS", 300),
        role_defaults={role: merged.get(f"AIFLOW_{role.upper()}_DEFAULT", "") for role in ("developer", "reviewer", "qa")},
        auto_merge_repos=_list(merged, "AIFLOW_AUTO_MERGE_REPOS", "*"),
        ci_required_repos=_list(merged, "AIFLOW_CI_REQUIRED_REPOS", ""),
        ci_wait_minutes=_int(merged, "AIFLOW_CI_WAIT_MINUTES", 30),
        feedback_settle_minutes=_int(merged, "AIFLOW_FEEDBACK_SETTLE_MINUTES", 5),
        gate_poll_seconds=_int(merged, "AIFLOW_GATE_POLL_SECONDS", 60),
        max_cycles=_int(merged, "AIFLOW_MAX_CYCLES", 6),
        merge_method=merged.get("AIFLOW_MERGE_METHOD", "merge"),
        ignored_authors=tuple(a.lower() for a in _list(merged, "AIFLOW_IGNORED_AUTHORS",
                              "codecov-commenter,github-actions,sonarqubecloud,sonarcloud,dependabot,codeql,github-advanced-security")),
        workspace_field=merged.get("AIFLOW_WORKSPACE_FIELD", "customfield_10214"),
        blocked_reason_field=merged.get("AIFLOW_BLOCKED_REASON_FIELD", "customfield_10215"),
        require_human_field=merged.get("AIFLOW_REQUIRE_HUMAN_FIELD", "customfield_10216"),
        model_field=merged.get("AIFLOW_MODEL_FIELD", "customfield_10217"),
        opencode_model=merged.get("AIFLOW_OPENCODE_MODEL", "opencode/deepseek-v4.1-flash-free"),
        explicit_only=_list(merged, "AIFLOW_EXPLICIT_ONLY_AIS", "OpenCode"),
        quota_block_minutes=_int(merged, "AIFLOW_QUOTA_BLOCK_MINUTES", 30),
        recheck_seconds=_int(merged, "AIFLOW_RECHECK_SECONDS", 120),
        workspace_name=merged.get("AIFLOW_WORKSPACE_NAME", {"wsl": "WSL", "mac": "Mac"}.get(machine, machine)),
    )
