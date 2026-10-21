"""Settings from the environment (AIFLOW4_*) plus files named by the environment.

Secrets are read from files (env files written on the machine, never from the control repo) so they never
appear in process arguments or in the repository.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def read_env_file(path: str | Path) -> dict[str, str]:
    """KEY=value lines; quotes around the value are stripped; missing file gives {}."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"AIFLOW4_{name}", default)


@dataclass
class Settings:
    # shared
    env: str = "lab"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_prefix: str = "aiflow4:"
    lease_ms: int = 90_000
    # control plane
    database_url: str = ""
    jira_site: str = "https://jooservices.atlassian.net"
    jira_email: str = ""
    jira_token: str = ""
    jira_projects: list[str] = field(default_factory=list)
    github_org: str = "jooservices"
    github_app_dir: str = "/etc/aiflow/secrets/github"
    control_repo: str = "/srv/aiflow/control/repos/aiflow-control"
    join_token: str = ""
    console_session_secret: str = ""
    o2_url: str = ""
    o2_basic: str = ""
    public_url: str = "http://127.0.0.1:18480"
    gatus_url: str = "/gatus/"  # Gatus status page, linked from the console footer
    # node / worker
    control_url: str = "http://10.44.0.10"
    node_name: str = ""
    node_token_file: str = "/srv/aiflow/state/node.token"
    srv_root: str = "/srv/aiflow"
    heartbeat_s: int = 30
    poll_s: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.env = _env("ENV", s.env)
        s.redis_url = _env("REDIS_URL", s.redis_url)
        s.lease_ms = int(_env("LEASE_MS", str(s.lease_ms)))
        s.database_url = _env("DATABASE_URL", s.database_url)
        s.jira_site = _env("JIRA_SITE", s.jira_site)
        s.jira_projects = [p for p in _env("JIRA_PROJECTS", "").split(",") if p]  # empty: every project in the control repo
        s.github_org = _env("GITHUB_ORG", s.github_org)
        s.github_app_dir = _env("GITHUB_APP_DIR", s.github_app_dir)
        s.control_repo = _env("CONTROL_REPO", s.control_repo)
        s.console_session_secret = _env("CONSOLE_SESSION_SECRET", s.console_session_secret)
        s.o2_url = _env("O2_URL", s.o2_url)
        s.public_url = _env("PUBLIC_URL", s.public_url)
        s.gatus_url = _env("GATUS_URL", s.gatus_url)
        s.control_url = _env("CONTROL_URL", s.control_url)
        s.node_name = _env("NODE_NAME", s.node_name)
        s.node_token_file = _env("NODE_TOKEN_FILE", s.node_token_file)
        s.srv_root = _env("SRV_ROOT", s.srv_root)
        s.heartbeat_s = int(_env("HEARTBEAT_S", str(s.heartbeat_s)))
        s.poll_s = int(_env("POLL_S", str(s.poll_s)))
        # secrets: from files named in the environment (or plain variables in tests)
        secrets: dict[str, str] = {}
        for path in _env("SECRET_FILES", "").split(","):
            if path:
                secrets.update(read_env_file(path))
        s.jira_email = os.environ.get("JIRA_EMAIL") or secrets.get("JIRA_EMAIL", "")
        s.jira_token = os.environ.get("JIRA_API_TOKEN") or secrets.get("JIRA_API_TOKEN", "")
        s.join_token = os.environ.get("AIFLOW4_JOIN_TOKEN") or secrets.get("AIFLOW4_JOIN_TOKEN", "")
        s.o2_basic = os.environ.get("O2_BASIC") or secrets.get("O2_BASIC", "")
        return s
