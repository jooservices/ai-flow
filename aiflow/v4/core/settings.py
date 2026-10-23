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
    # Jira connected on the Integrations screen (docs/v4/jira-connect.md): secrets live in files under this
    # directory, never in the registry; the OAuth endpoints are Atlassian's unless overridden (tests, proxies).
    jira_secret_dir: str = "/etc/aiflow/secrets/jira"
    jira_oauth_authorize_url: str = "https://auth.atlassian.com/authorize"
    jira_oauth_token_url: str = "https://auth.atlassian.com/oauth/token"
    jira_oauth_revoke_url: str = "https://auth.atlassian.com/oauth/revoke"
    jira_oauth_resources_url: str = "https://api.atlassian.com/oauth/token/accessible-resources"
    jira_api_base: str = "https://api.atlassian.com/ex/jira"
    canary_bug_project: str = "TESTFLOW"
    maintenance_project: str = "JAIF"  # the AI Flow project: tooling tickets (docs/v4/missing-tools.md)
    github_org: str = "jooservices"
    github_app_dir: str = "/etc/aiflow/secrets/github"
    control_repo: str = "/srv/aiflow/control/repos/aiflow-control"
    join_token: str = ""
    console_session_secret: str = ""
    console_login: bool = False  # AIFLOW4_CONSOLE_LOGIN=1: the console pages and read APIs need a signed-in user
    trusted_proxies: list[str] = field(default_factory=list)  # CIDRs allowed to supply forwarding headers
    console_require_mfa: bool = True  # AIFLOW4_CONSOLE_REQUIRE_MFA=0 explicitly disables the default requirement
    rate_limit_per_min: int = 600  # requests per client IP per minute on every endpoint (0 turns limits off)
    rate_limit_public_per_min: int = 60  # requests per client IP per minute on each public (no sign-in) endpoint
    reviewer_token: str = ""
    owner_token: str = ""
    o2_url: str = ""
    o2_basic: str = ""
    o2_source: str = "control"
    public_url: str = "http://127.0.0.1:18480"
    join_control_url: str = ""
    gatus_url: str = "/gatus/"  # Gatus status page, linked from the console footer
    # fleet upgrades (docs/v4/fleet-upgrade.md)
    control_version: str = ""  # the control plane's release (AIFLOW4_VERSION); not vX.Y.Z disables the skew policy
    rollout_batch_size: int = 2  # nodes per wave after the canary
    rollout_install_timeout_s: int = 1800  # a node must report the requested release within this time
    rollout_soak_s: int = 120  # and keep heartbeating on it this long to pass its health check
    rollout_canary_node: str = ""  # preferred canary; empty picks a node by name
    version_max_minor_lag: int = 1  # how many minors a node may run behind the control plane
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
        s.jira_secret_dir = _env("JIRA_SECRET_DIR", s.jira_secret_dir)
        s.jira_oauth_authorize_url = _env("JIRA_OAUTH_AUTHORIZE_URL", s.jira_oauth_authorize_url)
        s.jira_oauth_token_url = _env("JIRA_OAUTH_TOKEN_URL", s.jira_oauth_token_url)
        s.jira_oauth_revoke_url = _env("JIRA_OAUTH_REVOKE_URL", s.jira_oauth_revoke_url)
        s.jira_oauth_resources_url = _env("JIRA_OAUTH_RESOURCES_URL", s.jira_oauth_resources_url)
        s.jira_api_base = _env("JIRA_API_BASE", s.jira_api_base)
        s.canary_bug_project = _env("CANARY_BUG_PROJECT", s.canary_bug_project)
        s.maintenance_project = _env("MAINTENANCE_PROJECT", s.maintenance_project)
        s.github_org = _env("GITHUB_ORG", s.github_org)
        s.github_app_dir = _env("GITHUB_APP_DIR", s.github_app_dir)
        s.control_repo = _env("CONTROL_REPO", s.control_repo)
        s.console_session_secret = _env("CONSOLE_SESSION_SECRET", s.console_session_secret)
        s.console_login = _env("CONSOLE_LOGIN", "0").strip().lower() in ("1", "true", "yes", "on")
        s.trusted_proxies = [value.strip() for value in _env("TRUSTED_PROXIES", "").split(",") if value.strip()]
        s.console_require_mfa = _env("CONSOLE_REQUIRE_MFA", "1").strip().lower() in ("1", "true", "yes", "on")
        s.rate_limit_per_min = max(0, int(_env("RATE_LIMIT_PER_MIN", str(s.rate_limit_per_min))))
        s.rate_limit_public_per_min = max(0, int(_env("RATE_LIMIT_PUBLIC_PER_MIN", str(s.rate_limit_public_per_min))))
        s.o2_url = _env("O2_URL", s.o2_url)
        s.public_url = _env("PUBLIC_URL", s.public_url)
        s.join_control_url = _env("JOIN_CONTROL_URL", s.join_control_url)
        s.gatus_url = _env("GATUS_URL", s.gatus_url)
        s.control_version = _env("VERSION", s.control_version)
        s.o2_source = _env("O2_SOURCE", s.o2_source)
        s.rollout_batch_size = max(1, int(_env("ROLLOUT_BATCH_SIZE", str(s.rollout_batch_size))))
        s.rollout_install_timeout_s = int(_env("ROLLOUT_INSTALL_TIMEOUT_S", str(s.rollout_install_timeout_s)))
        s.rollout_soak_s = int(_env("ROLLOUT_SOAK_S", str(s.rollout_soak_s)))
        s.rollout_canary_node = _env("ROLLOUT_CANARY_NODE", _env("CANARY_NODE", s.rollout_canary_node))
        s.version_max_minor_lag = max(0, int(_env("VERSION_MAX_MINOR_LAG", str(s.version_max_minor_lag))))
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
        s.reviewer_token = os.environ.get("AIFLOW4_REVIEWER_TOKEN") or secrets.get("AIFLOW4_REVIEWER_TOKEN", "")
        s.owner_token = os.environ.get("AIFLOW4_OWNER_TOKEN") or secrets.get("AIFLOW4_OWNER_TOKEN", "")
        s.o2_basic = os.environ.get("O2_BASIC") or secrets.get("O2_BASIC", "")
        return s
