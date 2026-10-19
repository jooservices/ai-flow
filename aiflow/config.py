"""Settings for the AI Flow supervisor. Everything comes from the environment or `.env`."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
WORKSPACES_ROOT = Path(os.environ.get("AIFLOW_ROOT", PROJECT_DIR.parents[2])).resolve()


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
    poll_seconds: int
    heartbeat_seconds: int
    lease_seconds: int
    idle_minutes: int
    max_minutes: int
    orphan_grace_seconds: int
    max_attempts: int
    quota_cooldown_minutes: int
    redis_container: str
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
    workspace_name: str

    @property
    def owner(self) -> str:
        return f"{self.machine}:{self.slot}:{os.getpid()}"


def load(env: dict[str, str] | None = None) -> Settings:
    if env is None:  # an explicit environment (tests) must not pick up the machine's .env files
        merged = dict(read_env_file(WORKSPACES_ROOT / ".env"))
        merged.update(read_env_file(PROJECT_DIR / ".env"))
        merged.update(os.environ)
    else:
        merged = dict(env)
    machine = merged.get("AIFLOW_MACHINE", socket.gethostname().lower())
    return Settings(
        root=WORKSPACES_ROOT,
        projects=tuple(p.strip() for p in merged.get("AIFLOW_PROJECTS", "JAIF").split(",") if p.strip()),
        machine=machine,
        slot=_int(merged, "AIFLOW_SLOT", 1),
        poll_seconds=_int(merged, "AIFLOW_POLL_SECONDS", 30),
        heartbeat_seconds=_int(merged, "AIFLOW_HEARTBEAT_SECONDS", 20),
        lease_seconds=_int(merged, "AIFLOW_LEASE_SECONDS", 120),
        idle_minutes=_int(merged, "AIFLOW_IDLE_MINUTES", 30),
        max_minutes=_int(merged, "AIFLOW_MAX_MINUTES", 180),
        orphan_grace_seconds=_int(merged, "AIFLOW_ORPHAN_GRACE_SECONDS", 180),
        max_attempts=_int(merged, "AIFLOW_MAX_ATTEMPTS", 3),
        quota_cooldown_minutes=_int(merged, "AIFLOW_QUOTA_COOLDOWN_MINUTES", 30),
        redis_container=merged.get("AIFLOW_REDIS_CONTAINER", "workspaces-shared-redis-1"),
        redis_db=_int(merged, "AIFLOW_REDIS_DB", int(merged.get("REDIS_DB", 0))),
        redis_prefix=merged.get("AIFLOW_REDIS_PREFIX", "aiflow:"),
        redis_password=merged.get("WORKSPACES_REDIS_PASSWORD", ""),
        default_repo=merged.get("AIFLOW_DEFAULT_REPO", "ai-flow"),
        default_base=merged.get("AIFLOW_DEFAULT_BASE", "develop"),
        agent_order=tuple(a.strip() for a in merged.get("AIFLOW_AGENT_ORDER", "Claude,Codex,OpenCode").split(",") if a.strip()),
        wait_note_minutes=_int(merged, "AIFLOW_WAIT_NOTE_MINUTES", 20),
        expected_slots=tuple(r.strip() for r in merged.get("AIFLOW_EXPECTED_SLOTS", "wsl:1,wsl:2").split(",") if r.strip()),
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
        workspace_name=merged.get("AIFLOW_WORKSPACE_NAME", {"wsl": "WSL", "mac": "Mac"}.get(machine, machine)),
    )
