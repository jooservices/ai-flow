"""Health check: alert on the health ticket when a supervisor stops beating or when work is blocked."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone

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
    if redis.get("paused") is not None:
        found.append(("paused", "AI Flow is paused: no new ticket is taken (`bin/aiflow-work resume`)."))
    for name in redis.scan("probe:*"):
        state = redis.get(name) or ""
        if state.startswith("down"):
            _, machine, agent = name.split(":", 2)
            found.append((f"ai-{machine}-{agent}", f"{agent} on {machine} cannot work and is out of the pool: {state}"))
    for name in redis.scan("dbcleanup:*"):
        key = name.removeprefix("dbcleanup:")
        found.append((f"dbcleanup-{key}", f"Ticket database teardown failed for {key}; shared cleanup needs attention."))
    found += version_drift(redis)
    watched = sorted(cfg.projects)
    blocked = jira.search(f"project in ({', '.join(watched)}) AND status = Blocked", 20)
    if blocked:
        keys = ", ".join(issue["key"] for issue in blocked)
        found.append(("blocked", f"Tickets waiting for the owner (Blocked): {keys}."))
    for name in sorted(redis.scan("deploy-failure:*")):
        project_key = name.split(":", 1)[1]
        try:
            state = json.loads(redis.get(name) or "{}")
            ticket = state.get("ticket", "unknown ticket")
            outcome = state.get("outcome", "failed")
            log_file = state.get("log", "unknown log")
        except (TypeError, ValueError):
            ticket, outcome, log_file = "unknown ticket", "failed", "unknown log"
        found.append((f"deploy-{project_key}", f"Post-merge command for {project_key} failed after {ticket} ({outcome}); log: {log_file}."))
    return found


def version_drift(redis) -> list[tuple[str, str]]:
    """Tools whose versions differ between the machines that published them."""
    machines = {name.split(":", 1)[1]: json.loads(redis.get(name) or "{}") for name in redis.scan("versions:*")}
    if len(machines) < 2:
        return []
    found = []
    for tool in sorted({tool for versions in machines.values() for tool in versions}):
        seen = {machine: versions.get(tool, "missing") for machine, versions in machines.items()}
        if len(set(seen.values())) > 1:
            found.append((f"drift-{tool}", f"{tool} differs between machines: " + ", ".join(f"{m} {v}" for m, v in sorted(seen.items()))))
    return found


def usage_summary(redis, day: str) -> str | None:
    """One line per AI and role for the runs of `day` (UTC): runs, minutes, runs that did not end cleanly."""
    names = redis.scan(f"usage:{day}:*")
    if not names:
        return None
    totals: dict[tuple[str, str], list[float]] = {}
    for raw in redis.get_many(names):
        if not raw:
            continue
        run = json.loads(raw)
        row = totals.setdefault((run["ai"], run["role"]), [0, 0.0, 0])
        row[0] += 1
        row[1] += run["minutes"]
        row[2] += 0 if run["why"] == "ok" else 1
    lines = [f"- {ai} {role}: {int(row[0])} run(s), {row[1]:.0f} min, {int(row[2])} not clean" for (ai, role), row in sorted(totals.items())]
    return f"AI usage on {day} (UTC):\n" + "\n".join(lines)


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
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    summary = usage_summary(redis, yesterday)
    if summary and cfg.health_ticket and redis.acquire(f"usage-report:{yesterday}", "health", 3 * 86400):
        jira.comment(cfg.health_ticket, summary)
    if not found:
        print("OK all expected supervisors are beating and nothing is blocked")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
