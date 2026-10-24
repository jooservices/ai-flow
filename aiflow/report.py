"""Read-only report of what happened in AI Flow, from the events in OpenObserve.

    bin/aiflow-report [--hours 24] [--json] [--warn 60] [--limit 120]

Sections: Blocked tickets by incident signature (the same cause counted once), time per status, steps no AI could take,
and tickets whose AI minutes passed the warning or the limit. AI minutes are the sum of `ai.run` minutes; CI wait is not an AI run."""
from __future__ import annotations

import argparse
import base64
import json
import re
import time
import urllib.request
from collections import Counter, defaultdict
from typing import Any, Callable

from . import audit_common as common

KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
PATH = re.compile(r"(?:/[\w.\-@]+){2,}")
SLUG = re.compile(r"<KEY>(?:-[A-Za-z0-9]+)+")
NUMBER = re.compile(r"\b\d+\b")
LIMIT_ROWS = 5000
MAX_ROWS = 200_000


def normalize(reason: str) -> str:
    """The reason without ticket keys, paths and numbers, so the same cause gets the same text."""
    text = KEY.sub("<KEY>", reason or "")
    text = SLUG.sub("<KEY>", text)  # a worktree or branch name is the ticket key plus a slug of its title
    text = PATH.sub("<PATH>", text)
    text = NUMBER.sub("<N>", text)
    return re.sub(r"\s+", " ", text).strip()[:100]


def signature(kind: str, reason: str) -> str:
    return f"{kind or 'Other'}:{normalize(reason)}"


def query(url: str, org: str, stream: str, user: str, password: str, sql: str, hours: int,
          opener: Callable[..., Any] = urllib.request.urlopen) -> list[dict[str, Any]]:
    end = int(time.time() * 1_000_000)
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    rows: list[dict[str, Any]] = []
    while True:  # page through the raw rows of one fixed window; aggregate queries fit in the first page
        body = json.dumps({"query": {"sql": sql.replace("{stream}", stream), "start_time": end - hours * 3600 * 1_000_000, "end_time": end,
                                     "from": len(rows), "size": LIMIT_ROWS}}).encode()
        request = urllib.request.Request(f"{url.rstrip('/')}/api/{org}/_search", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
        with opener(request, timeout=30) as response:
            page = json.load(response).get("hits", [])
        rows += page
        if len(page) < LIMIT_ROWS or len(rows) >= MAX_ROWS:
            return rows


def by_signature(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        sig = signature(row.get("kind", ""), row.get("reason", ""))
        group = groups.setdefault(sig, {"signature": sig, "count": 0, "tickets": set(), "projects": set(), "machines": set()})
        group["count"] += 1 if not row.get("ticket") or row["ticket"] not in group["tickets"] else 0
        for name, field in (("tickets", "ticket"), ("projects", "project"), ("machines", "machine")):
            if row.get(field):
                group[name].add(row[field])
    out = []
    for group in groups.values():
        out.append({**group, "tickets": sorted(group["tickets"]), "projects": sorted(group["projects"]), "machines": sorted(group["machines"])})
    return sorted(out, key=lambda item: -item["count"])


def over_limits(rows: list[dict[str, Any]], warn: float, limit: float) -> list[dict[str, Any]]:
    total: dict[str, float] = defaultdict(float)
    longest: dict[str, float] = defaultdict(float)
    runs: Counter = Counter()
    for row in rows:
        ticket, minutes = row.get("ticket"), float(row.get("minutes") or 0)
        if ticket:
            total[ticket] += minutes
            longest[ticket] = max(longest[ticket], minutes)
            runs[ticket] += 1
    found = [{"ticket": t, "ai_minutes": round(m, 1), "longest_run": round(longest[t], 1), "runs": runs[t],
              "level": "over-limit" if m > limit else "warn"} for t, m in total.items() if m > min(warn, limit)]
    return sorted(found, key=lambda item: -item["ai_minutes"])


def build(search: Callable[[str], list[dict[str, Any]]], warn: float, limit: float) -> dict[str, Any]:
    return {
        "blocked": by_signature(search("select ticket, project, machine, kind, reason from {stream} where event = 'ticket.blocked'")),
        "status_minutes": search("select from_status, count(*) as moves, round(avg(minutes_in_from), 1) as avg_minutes from {stream} "
                                 "where event = 'ticket.status' and from_status <> '' group by from_status order by moves desc"),
        "unserved": search("select role, count(*) as times from {stream} where event = 'ai.unserved' group by role order by times desc"),
        "tickets_over": over_limits(search("select ticket, minutes from {stream} where event = 'ai.run'"), warn, limit),
        "done": search("select project, count(*) as done from {stream} where event = 'ticket.done' group by project"),
    }


def render(data: dict[str, Any], hours: int, warn: float, limit: float) -> str:
    lines = [f"AI Flow report, last {hours} h"]
    lines.append("\nBlocked by signature (count, tickets):")
    lines += [f"  {item['count']:>3}  {item['signature']}  [{', '.join(item['projects'])}] {', '.join(item['tickets'][:4])}" for item in data["blocked"]] or ["  none"]
    lines.append("\nAverage minutes in a status before it was left:")
    lines += [f"  {row['from_status']:<16} {row['moves']:>4} moves  avg {row['avg_minutes']} min" for row in data["status_minutes"]] or ["  no data"]
    lines.append("\nSteps no AI could take:")
    lines += [f"  {row['role']:<10} {row['times']}" for row in data["unserved"]] or ["  none"]
    lines.append(f"\nTickets over {warn:g} AI minutes (limit {limit:g}):")
    lines += [f"  {item['ticket']:<12} {item['ai_minutes']:>7} min  runs {item['runs']}  longest {item['longest_run']}  {item['level']}" for item in data["tickets_over"]] or ["  none"]
    lines.append("\nDone: " + (", ".join(f"{row['project']} {row['done']}" for row in data["done"]) or "none"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only AI Flow report from OpenObserve events.")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--warn", type=float, default=60, help="AI minutes of a ticket that are reported")
    parser.add_argument("--limit", type=float, default=120, help="AI minutes of a ticket that count as over the limit")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.hours <= 24 * 30:
        parser.error("--hours must be between 1 and 720")
    root = common.workspaces_root()
    env, runtime = common.env_values(root), common.runtime(root)
    url = runtime.get("obs_url", "")
    user, password = env.get("AIFLOW_OBS_USER", ""), env.get("AIFLOW_OBS_PASSWORD", "")
    if not (url and user and password):
        print("OpenObserve is not configured (obs_url in aiflow.toml, AIFLOW_OBS_USER/PASSWORD in the root .env).")
        return 2
    org, stream = runtime.get("obs_org", "default"), runtime.get("obs_stream", "aiflow_events")
    data = build(lambda sql: query(url, org, stream, user, password, sql, args.hours), args.warn, args.limit)
    print(json.dumps(data, indent=2) if args.json else render(data, args.hours, args.warn, args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
