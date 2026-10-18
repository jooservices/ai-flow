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

    @property
    def owner(self) -> str:
        return f"{self.machine}:{self.slot}:{os.getpid()}"


def load(env: dict[str, str] | None = None) -> Settings:
    merged = dict(read_env_file(WORKSPACES_ROOT / ".env"))
    merged.update(read_env_file(PROJECT_DIR / ".env"))
    merged.update(os.environ if env is None else env)
    return Settings(
        root=WORKSPACES_ROOT,
        projects=tuple(p.strip() for p in merged.get("AIFLOW_PROJECTS", "JAIF").split(",") if p.strip()),
        machine=merged.get("AIFLOW_MACHINE", socket.gethostname().lower()),
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
    )
