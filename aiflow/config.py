"""Settings for the AI Flow supervisor. Everything comes from the environment or `.env`."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .projects import Registry

PROJECT_DIR = Path(__file__).resolve().parents[1]
WORKSPACES_ROOT = Path(os.environ.get("AIFLOW_ROOT", PROJECT_DIR.parents[2])).resolve()


CONFIG_DIR = Path(os.environ.get("AIFLOW_CONFIG_DIR", WORKSPACES_ROOT / "JOOservices" / "config")).resolve()


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
    workers: int
    registry: Registry
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
    type_field: str
    probe_hours: int
    version_field: str
    opencode_model: str
    explicit_only: tuple[str, ...]
    quota_block_minutes: int
    recheck_seconds: int
    workspace_name: str
    notify: dict = field(default_factory=dict)  # AIFLOW_NOTIFY_* and JIRA_URL, for the operator's messages to the owner
    operator_policy: Any = None  # aiflow.operator_policy.Policy

    @property
    def worker_label(self) -> str:
        return str(self.slot)

    @property
    def owner(self) -> str:
        return f"{self.machine}:{self.worker_label}:{os.getpid()}"

    @property
    def ai_limits(self) -> dict[str, int]:
        """Concurrent AI session limits for this machine; an omitted AI has no limit."""
        return self.registry.ai_limits.get(self.machine, {})

    def project(self, issue_key: str = ""):
        """The registry entry of the Jira project an issue belongs to (the first project when no issue is given)."""
        return self.registry.of_issue(issue_key) if issue_key else next(iter(self.registry.projects.values()))


def parse_worker_id(worker_id: str) -> int:
    if not worker_id.isdigit() or int(worker_id) < 1:
        raise ConfigError(f"Worker id `{worker_id}` must be a whole number of at least 1.")
    return int(worker_id)


def expected_workers(merged: dict[str, str], machine: str, registry: Registry) -> tuple[str, ...]:
    """Heartbeats the health check expects on this machine: one per worker of the registry, otherwise the legacy
    AIFLOW_EXPECTED_SLOTS list."""
    if machine in registry.workers:
        return tuple(f"{machine}:{n}" for n in range(1, registry.workers[machine] + 1))
    return tuple(r.strip() for r in merged.get("AIFLOW_EXPECTED_SLOTS", "wsl:1,wsl:2").split(",") if r.strip())


def load(env: dict[str, str] | None = None, worker_id: str | None = None, config_dir: Path | None = None) -> Settings:
    """Settings of one worker. `worker_id` is the worker number on this machine (`2`); without it AIFLOW_SLOT applies.
    Project settings come from the TOML files of `config_dir` (see projects.py); a machine without them uses `.env`."""
    from . import operator_policy, projects as registry_module
    if env is None:  # an explicit environment (tests) must not pick up the machine's .env files
        merged = dict(read_env_file(WORKSPACES_ROOT / ".env"))
        merged.update(read_env_file(WORKSPACES_ROOT / "JOOservices" / ".env"))
        merged.update(read_env_file(PROJECT_DIR / ".env"))
        shell = dict(os.environ)
    else:
        merged = dict(env)
        shell = {}
    merged.update(shell)
    if worker_id is not None:
        merged["AIFLOW_SLOT"] = str(parse_worker_id(worker_id))
    machine = merged.get("AIFLOW_MACHINE", socket.gethostname().lower())
    role_defaults = {role: merged.get(f"AIFLOW_{role.upper()}_DEFAULT", "") for role in ("developer", "reviewer", "qa")}
    default_repo = merged.get("AIFLOW_DEFAULT_REPO", "ai-flow")
    auto_merge_repos = _list(merged, "AIFLOW_AUTO_MERGE_REPOS", "*")
    ci_required_repos = _list(merged, "AIFLOW_CI_REQUIRED_REPOS", "")
    fallback = registry_module.Project(
        jira_key="", repo=default_repo, base=merged.get("AIFLOW_DEFAULT_BASE", "develop"), developer=role_defaults["developer"],
        reviewer=role_defaults["reviewer"], qa=role_defaults["qa"], auto_merge=repo_listed(auto_merge_repos, default_repo),
        ci_required=repo_listed(ci_required_repos, default_repo), merge_method=merged.get("AIFLOW_MERGE_METHOD", "merge"))
    directory = config_dir or (CONFIG_DIR if env is None else None)  # an explicit environment (tests) reads no machine files
    registry = (registry_module.load(directory, fallback) if directory else None) or registry_module.legacy(
        tuple(p.strip() for p in merged.get("AIFLOW_PROJECTS", "JAIF").split(",") if p.strip()), fallback)
    merged.update(registry.runtime)  # [runtime] of aiflow.toml beats the legacy .env value
    slot = _int(merged, "AIFLOW_SLOT", 1)
    workers = registry.workers.get(machine, 0)
    if worker_id is not None and workers and slot > workers:
        raise ConfigError(f"Machine {machine} runs {workers} worker(s) in aiflow.toml; worker {slot} is outside that number.")
    return Settings(
        root=WORKSPACES_ROOT,
        projects=tuple(registry.projects),
        machine=machine,
        slot=slot,
        workers=workers or 1,
        registry=registry,
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
        default_repo=default_repo,
        default_base=merged.get("AIFLOW_DEFAULT_BASE", "develop"),
        agent_order=tuple(a.strip() for a in merged.get("AIFLOW_AGENT_ORDER", "Claude,Codex,OpenCode").split(",") if a.strip()),
        wait_note_minutes=_int(merged, "AIFLOW_WAIT_NOTE_MINUTES", 10),
        expected_slots=expected_workers(merged, machine, registry),
        health_ticket=merged.get("AIFLOW_HEALTH_TICKET", ""),
        heartbeat_stale_seconds=_int(merged, "AIFLOW_HEARTBEAT_STALE_SECONDS", 300),
        role_defaults=role_defaults,
        auto_merge_repos=auto_merge_repos,
        ci_required_repos=ci_required_repos,
        ci_wait_minutes=_int(merged, "AIFLOW_CI_WAIT_MINUTES", 90),
        feedback_settle_minutes=_int(merged, "AIFLOW_FEEDBACK_SETTLE_MINUTES", 5),
        gate_poll_seconds=_int(merged, "AIFLOW_GATE_POLL_SECONDS", 60),
        max_cycles=_int(merged, "AIFLOW_MAX_CYCLES", 6),
        merge_method=merged.get("AIFLOW_MERGE_METHOD", "merge"),
        ignored_authors=tuple(a.lower() for a in _list(merged, "AIFLOW_IGNORED_AUTHORS",
                              "codecov-commenter,github-actions,sonarqubecloud,sonarcloud,dependabot,codeql,github-advanced-security")),
        workspace_field=merged.get("AIFLOW_WORKSPACE_FIELD", "Workspace"),
        blocked_reason_field=merged.get("AIFLOW_BLOCKED_REASON_FIELD", "Blocked reason"),
        require_human_field=merged.get("AIFLOW_REQUIRE_HUMAN_FIELD", "Require human"),
        model_field=merged.get("AIFLOW_MODEL_FIELD", "AI Model"),
        type_field=merged.get("AIFLOW_TYPE_FIELD", "Type"),
        probe_hours=_int(merged, "AIFLOW_PROBE_HOURS", 3),
        version_field=merged.get("AIFLOW_VERSION_FIELD", "Version"),
        opencode_model=merged.get("AIFLOW_OPENCODE_MODEL", "opencode/deepseek-v4.1-flash-free"),
        explicit_only=_list(merged, "AIFLOW_EXPLICIT_ONLY_AIS", "OpenCode"),
        quota_block_minutes=_int(merged, "AIFLOW_QUOTA_BLOCK_MINUTES", 30),
        recheck_seconds=_int(merged, "AIFLOW_RECHECK_SECONDS", 120),
        workspace_name=merged.get("AIFLOW_WORKSPACE_NAME", {"wsl": "WSL", "mac": "Mac"}.get(machine, machine)),
        notify={k: v for k, v in merged.items() if k.startswith("AIFLOW_NOTIFY_") or k == "JIRA_URL"},
        operator_policy=operator_policy.load(directory),
    )
