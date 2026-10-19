"""Health check: alert on the health ticket when a supervisor stops beating or when work is blocked."""
from __future__ import annotations

import sys
import time

from .config import load
from .jira import Jira
from .lock import Redis

HOUR = 3600


def problems(cfg, redis, jira) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    now = int(time.time())
    for slot in cfg.expected_slots:
        beat = redis.get(f"hb:{slot}")
        if beat is None or now - int(beat) > cfg.heartbeat_stale_seconds:
            found.append((f"slot-{slot}", f"Supervisor {slot} has not sent a heartbeat for over {cfg.heartbeat_stale_seconds // 60} min."))
    watched = sorted(cfg.projects)
    blocked = jira.search(f"project in ({', '.join(watched)}) AND status = Blocked", 20)
    if blocked:
        keys = ", ".join(issue["key"] for issue in blocked)
        found.append(("blocked", f"Tickets waiting for the owner (Blocked): {keys}."))
    return found


def main() -> int:
    cfg = load()
    redis = Redis(cfg.redis_host, cfg.redis_port, cfg.redis_db, cfg.redis_prefix, cfg.redis_password, cfg.redis_user)
    jira = Jira(cfg.root)
    found = problems(cfg, redis, jira)
    for kind, message in found:
        print(f"PROBLEM {kind}: {message}")
        if cfg.health_ticket and redis.get(f"alert:{kind}") is None:
            redis.set(f"alert:{kind}", "1", HOUR)  # at most one alert per kind and hour
            jira.comment(cfg.health_ticket, f"AI Flow health alert from {cfg.machine}: {message}")
    if not found:
        print("OK all expected supervisors are beating and nothing is blocked")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
