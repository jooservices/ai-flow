"""A tool missing on a machine is a machine problem (docs/v4/missing-tools.md).

When a developer AI reports that a check cannot run because a tool or service is missing, the control plane
records the requirement and the machine, parks the ticket (it stays in To Do, hidden from machines that lack the
requirement) and offers it again to the first machine that has it. Nothing goes to the owner. When no machine
can supply a missing tool for 24 hours, one maintenance ticket labelled `tooling` is opened in the AI Flow
project. The same module keeps the per-machine result of the project check commands run on a clean checkout.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.services import ALIASES, SERVICES, service_gaps
from ..core.toolchains import (CATALOG_TOOLS, NODE_TOOLS, STACK_TOOLS, pinned_version, tool_present,
                               normalize_stacks, tools_for_stacks_in_project)
from .db import J

log = logging.getLogger("aiflow4.tooling")
MAINTENANCE_AFTER = timedelta(hours=24)
MAINTENANCE_LABEL = "tooling"


def normalize(requirement: str) -> str:
    name = str(requirement).strip().lower()
    return ALIASES.get(name, name)


def machine_has(node_row: dict, requirement: str) -> bool:
    """True when the machine reports the tool (or healthy test service) in its latest heartbeat."""
    name = normalize(requirement)
    if name in SERVICES:
        return not service_gaps(((node_row.get("metrics") or {}).get("services")), [name])
    return tool_present(node_row.get("tools"), name)


def machine_tools(node_row: dict) -> dict[str, list[str]]:
    """What the developer AI is told before it starts: the tools of its machine, available and not."""
    from ..core.toolchains import known_requirements
    available, missing = {}, []
    for name in known_requirements():
        if name in SERVICES:
            continue
        if machine_has(node_row, name):
            available[name] = str((node_row.get("tools") or {}).get(name, ""))[:80]
        else:
            missing.append(name)
    services = [s for s in SERVICES if machine_has(node_row, s)]
    return {"available": available, "missing": missing, "services": services}


def record_missing(ctx, run: dict, requirements: list[str], detail: str) -> list[str]:
    """Park the run's ticket on the requirements it lacks on the run's machine. Returns the recorded names."""
    node = ctx.db.one("SELECT tools,metrics FROM nodes WHERE name=%s", (run["node"],)) or {}
    names = list(dict.fromkeys(name for raw in requirements if str(raw).strip()
                               if (name := normalize(raw))
                               and requirement_is_accepted(ctx, run["project"], name)))[:10]
    recorded: list[str] = []
    for name in names:
        if machine_has(node, name):
            ctx.db.run("UPDATE tool_waits SET resolved_at=now(), resolution=%s WHERE ticket_key=%s"
                       " AND requirement=%s AND resolved_at IS NULL",
                       (f"already present on {run['node']}", run["ticket_key"], name))
            continue
        ctx.db.run("INSERT INTO tool_waits (ticket_key, project, requirement, machine, run_id, detail)"
                   " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (ticket_key, requirement) WHERE resolved_at IS NULL"
                   " DO UPDATE SET machine=EXCLUDED.machine, run_id=EXCLUDED.run_id, detail=EXCLUDED.detail",
                   (run["ticket_key"], run["project"], name, run["node"], str(run["id"]), detail[:1000]))
        recorded.append(name)
    if recorded:
        ctx.events.emit("ticket.waiting_for_tool", ticket=run["ticket_key"], project=run["project"],
                        run_id=str(run["id"]), node=run["node"], outcome="waiting",
                        reason="missing on the machine: " + ", ".join(recorded))
    return recorded


def resolve_ticket(ctx, ticket_key: str, machine: str) -> None:
    """The ticket was offered to a machine that has what it waited for."""
    ctx.db.run("UPDATE tool_waits SET resolved_at=coalesce(resolved_at,now()), resolution=%s WHERE ticket_key=%s"
               " AND (resolved_at IS NULL OR resolution LIKE 'available on %%')",
               (f"offered to {machine}", ticket_key))


def resolve_available(ctx, node: str, node_row: dict) -> int:
    """A fresh heartbeat resolves waits this project-serving machine can now satisfy."""
    repo = ctx.repo()
    node_config = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name=%s", (node,)) or {}
    projects = set(repo.projects_for_node(node, node_config.get("projects") or [], node_config.get("dynamic", False)))
    projects.update(node_config.get("projects") or [])
    resolved = 0
    for wait in ctx.db.all("SELECT ticket_key,project,requirement FROM tool_waits WHERE resolved_at IS NULL"):
        if (not requirement_is_accepted(ctx, wait["project"], wait["requirement"])
                or wait["project"] not in projects
                or not machine_has(node_row, wait["requirement"])):
            continue
        ctx.db.run("UPDATE tool_waits SET resolved_at=now(),resolution=%s WHERE ticket_key=%s AND requirement=%s"
                   " AND resolved_at IS NULL", (f"available on {node}", wait["ticket_key"], wait["requirement"]))
        resolved += 1
    return resolved


def parked_tickets(ctx, node_row: dict, ticket_keys: list[str] | None = None) -> set[str]:
    """Tickets waiting for a requirement this machine does not have (they are not offered to it)."""
    if ticket_keys is not None and not ticket_keys:
        return set()
    parked: set[str] = set()
    cache: dict[str, bool] = {}
    sql = ("SELECT ticket_key, project, requirement FROM tool_waits WHERE (resolved_at IS NULL"
           " OR resolution LIKE 'available on %%')")
    params: tuple = ()
    if ticket_keys is not None:
        sql += " AND ticket_key=ANY(%s)"
        params = (ticket_keys,)
    for row in ctx.db.all(sql, params):
        req = row["requirement"]
        if not requirement_is_accepted(ctx, row["project"], req):
            continue
        if req not in cache:
            cache[req] = machine_has(node_row, req)
        if not cache[req]:
            parked.add(row["ticket_key"])
    return parked


def waiting_summary(ctx) -> list[dict[str, Any]]:
    """Tickets waiting per missing requirement, for Command Center."""
    rows = ctx.db.all("SELECT w.ticket_key, w.project, w.requirement, w.machine, w.created_at, w.detail, t.summary"
                      " FROM tool_waits w LEFT JOIN tickets t ON t.key = w.ticket_key"
                      " WHERE w.resolved_at IS NULL ORDER BY w.created_at, w.ticket_key")
    gaps = {g["requirement"]: g for g in ctx.db.all("SELECT * FROM tooling_gaps")}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not requirement_is_accepted(ctx, r["project"], r["requirement"]):
            continue
        project = ctx.repo().projects.get(r["project"])
        stacks = project.stacks if project else []
        action = system_action(r["requirement"], stacks)
        item = out.setdefault(r["requirement"], {"requirement": r["requirement"], "tickets": [], "projects": [],
                                                 "machines": [], "since": r["created_at"].isoformat(),
                                                 "action": system_action(r["requirement"])})
        item["tickets"].append({"key": r["ticket_key"], "summary": r["summary"] or "", "machine": r["machine"],
                                "requirement": r["requirement"], "since": r["created_at"].isoformat(),
                                "action": action, "detail": r["detail"] or ""})
        if r["project"] and r["project"] not in item["projects"]:
            item["projects"].append(r["project"])
        if r["machine"] and r["machine"] not in item["machines"]:
            item["machines"].append(r["machine"])
    for name, item in out.items():
        item["count"] = len(item["tickets"])
        gap = gaps.get(name) or {}
        item["maintenance_ticket"] = gap.get("maintenance_key") or ""
    return sorted(out.values(), key=lambda i: (-i["count"], i["requirement"]))


def requirement_catalogue() -> set[str]:
    """Reportable catalogue tools and services; other stack tools require an explicit pin."""
    return set(NODE_TOOLS) | set(CATALOG_TOOLS) | set(SERVICES)


def requirement_is_accepted(ctx, project_key: str, requirement: str) -> bool:
    """Catalogue tools/services are always valid; other stack tools need a project pin."""
    name = normalize(requirement)
    from ..core.services import EXTERNAL_REQUIREMENTS
    if name in EXTERNAL_REQUIREMENTS:
        return False
    if name in requirement_catalogue():
        return True
    repo = ctx.repo()
    project = repo.projects.get(project_key)
    if not project:
        return False
    stacks = project.stacks
    stack_tools = {tool for stack in normalize_stacks(stacks) for tool in STACK_TOOLS.get(stack, ())}
    return name in stack_tools and bool(pinned_version(repo.versions, name, stacks))


def system_action(requirement: str, stacks: str | list[str] | tuple[str, ...] | None = None) -> str:
    """Human-readable next action for a validated wait, shown in the Command Center and idle incident."""
    if requirement in SERVICES:
        return "Starting the declared test service on the next heartbeat"
    stack_tools = {tool for stack in normalize_stacks(stacks) for tool in STACK_TOOLS.get(stack, ())}
    if requirement in stack_tools:
        return "Installing the pinned project stack tool on the next heartbeat"
    if requirement in set(NODE_TOOLS) | set(CATALOG_TOOLS):
        return "Installing automatically on the next heartbeat on machines that report it missing"
    return "Waiting for a project machine to report it present"


def install_requests(ctx, node: str, node_row: dict, repo) -> list[dict[str, str]]:
    """Server-authored, whitelist-bound tool installs to include in this node's heartbeat response."""
    installable = set(NODE_TOOLS) | set(CATALOG_TOOLS) | {tool for tools in STACK_TOOLS.values() for tool in tools}
    waits = ctx.db.all("SELECT DISTINCT requirement,project FROM tool_waits WHERE resolved_at IS NULL"
                       " OR resolution LIKE 'available on %' ORDER BY requirement")
    requests: dict[str, str] = {}
    for wait in waits:
        name = wait["requirement"]
        if (name not in installable or not requirement_is_accepted(ctx, wait["project"], name)
                or machine_has(node_row, name)):
            continue
        project = repo.projects.get(wait["project"])
        stacks = project.stacks if project else []
        version = "" if name == "cosign" else pinned_version(repo.versions, name, stacks)
        if name in {"claude", "codex", "grok", "opencode", "codex-code-mode-host"} and not version:
            continue  # these signed-off CLI installers require a versions.lock pin
        requests[name] = version
    return [{"name": name, "version": requests[name]} for name in sorted(requests)]


def _online(ctx, rows: list[dict]) -> list[dict]:
    from .state import NODE_OFFLINE_S
    now = datetime.now(timezone.utc)
    return [n for n in rows if n["status"] == "online" and n["last_heartbeat"] is not None
            and (now - n["last_heartbeat"]).total_seconds() <= NODE_OFFLINE_S]


def declared_requirements(repo) -> set[str]:
    """Tools and services some project's declared stack asks the node agents to prepare."""
    declared: set[str] = set()
    for project in repo.projects.values():
        declared.update(tools_for_stacks_in_project(project.stacks, ()))
        declared.update(project.required_services)
    return declared


def sweep(ctx) -> list[str]:
    """Close finished waits, keep the 24 hour clocks and open the maintenance tickets. Returns the keys opened."""
    for wait in ctx.db.all("SELECT ticket_key,project,requirement FROM tool_waits WHERE resolved_at IS NULL"):
        if not requirement_is_accepted(ctx, wait["project"], wait["requirement"]):
            ctx.db.run("UPDATE tool_waits SET resolved_at=now(),resolution='unknown requirement ignored'"
                       " WHERE ticket_key=%s AND requirement=%s AND resolved_at IS NULL",
                       (wait["ticket_key"], wait["requirement"]))
    ctx.db.run("UPDATE tool_waits SET resolved_at=now(), resolution='ticket is done' WHERE resolved_at IS NULL"
               " AND ticket_key IN (SELECT key FROM tickets WHERE status='Done')")
    waiting = {r["requirement"]: r["since"] for r in ctx.db.all(
        "SELECT requirement, min(created_at) AS since FROM tool_waits WHERE resolved_at IS NULL GROUP BY requirement")}
    ctx.db.run("DELETE FROM tooling_gaps WHERE NOT (requirement = ANY(%s))", (list(waiting),))
    if not waiting:
        return []
    repo = ctx.repo()
    declared = declared_requirements(repo)
    online = _online(ctx, ctx.db.all("SELECT name,status,last_heartbeat,tools,metrics FROM nodes"))
    opened: list[str] = []
    for name, since in waiting.items():
        if name in declared or any(machine_has(n, name) for n in online):
            ctx.db.run("DELETE FROM tooling_gaps WHERE requirement=%s", (name,))  # a machine can supply it: restart the clock
            continue
        ctx.db.run("INSERT INTO tooling_gaps (requirement, since) VALUES (%s,%s) ON CONFLICT DO NOTHING", (name, since))
        gap = ctx.db.one("SELECT * FROM tooling_gaps WHERE requirement=%s", (name,))
        if gap["maintenance_key"] or datetime.now(timezone.utc) - gap["since"] < MAINTENANCE_AFTER:
            continue
        key = open_maintenance_ticket(ctx, name, gap["since"])
        if key:
            opened.append(key)
    return opened


def undeclared_service_waits(ctx) -> dict[str, list[str]]:
    """Open waits on a test service (postgres, redis) that the ticket's project does not declare: {project: services}."""
    repo = ctx.repo()
    out: dict[str, set[str]] = {}
    for wait in ctx.db.all("SELECT project,requirement FROM tool_waits WHERE resolved_at IS NULL"):
        name = normalize(wait["requirement"])
        project = repo.projects.get(wait["project"])
        if project and name in SERVICES and name not in project.required_services:
            out.setdefault(wait["project"], set()).add(name)
    return {key: sorted(names) for key, names in out.items()}


def declare_undeclared_services(ctx) -> list[str]:
    """Flow doctor: a ticket parked on a test service its project does not declare is never left parked. The service
    is declared for the project (machines prepare and report it on the next heartbeats and the wait ends); when that
    cannot be done, one alert per project says what to declare. Returns the projects declared."""
    from .api import declare_project_services
    from .incidents import close_incident, open_incident

    declared: list[str] = []
    alerts: set[str] = set()
    for key, missing in undeclared_service_waits(ctx).items():
        wanted = list(dict.fromkeys([*ctx.repo().projects[key].required_services, *missing]))
        try:
            declare_project_services(ctx, key, wanted, f"projects: declare test services of {key} (flow doctor)")
        except Exception as exc:
            log.warning("flow doctor could not declare services for %s (%s)", key, type(exc).__name__)
            alerts.add(key)
            open_incident(ctx, "service_undeclared", key, "warning", f"{key} tickets wait for undeclared test services",
                          f"Tickets of {key} are parked on {', '.join(missing)}, which the project does not declare, so no "
                          f"machine prepares or reports them. The flow doctor could not declare them itself. Add "
                          f"{', '.join(missing)} to Test services of {key} on the Projects screen; the incident closes when "
                          "the project declares them.")
            continue
        declared.append(key)
        ctx.events.emit("project.services_declared", project=key, outcome="declared",
                        reason=f"flow doctor declared {', '.join(missing)}")
    for i in ctx.db.all("SELECT subject FROM incidents WHERE kind='service_undeclared' AND closed_at IS NULL"):
        if i["subject"] not in alerts:
            close_incident(ctx, "service_undeclared", i["subject"], "the project declares the test services")
    return declared


def check_stalled_waits(ctx) -> None:
    """Warn once per ticket and requirement when a parked wait has made no progress for two hours."""
    from .incidents import close_incident, open_incident

    rows = ctx.db.all("SELECT ticket_key,project,requirement,machine,created_at,detail FROM tool_waits"
                      " WHERE resolved_at IS NULL AND created_at < now() - interval '2 hours'")
    current: set[str] = set()
    for row in rows:
        subject = f"tool_wait:{row['ticket_key']}:{row['requirement']}"
        current.add(subject)
        project = ctx.repo().projects.get(row["project"])
        action = system_action(row["requirement"], project.stacks if project else [])
        open_incident(ctx, "ticket_stuck", subject, "warning",
                      f"{row['ticket_key']} has waited over two hours for {row['requirement']}",
                      f"Project {row['project']}; missing on {row['machine'] or 'an unknown machine'} since "
                      f"{row['created_at']:%Y-%m-%d %H:%M} UTC. The system is {action.lower()}."
                      + (f" Last report: {row['detail'][:300]}" if row["detail"] else ""))
    for incident in ctx.db.all("SELECT subject FROM incidents WHERE kind='ticket_stuck'"
                               " AND subject LIKE 'tool_wait:%' AND closed_at IS NULL"):
        if incident["subject"] not in current:
            close_incident(ctx, "ticket_stuck", incident["subject"],
                           "the wait was resolved or the ticket was completed")


def open_maintenance_ticket(ctx, requirement: str, since: datetime) -> str:
    if not ctx.jira:
        return ""
    waits = ctx.db.all("SELECT ticket_key, project, machine FROM tool_waits WHERE requirement=%s AND resolved_at IS NULL"
                       " ORDER BY ticket_key", (requirement,))
    projects = sorted({w["project"] for w in waits if w["project"]})
    machines = sorted({w["machine"] for w in waits if w["machine"]})
    summary = f"[Tooling] Install {requirement} on a machine: no machine can supply it"
    description = ("Opened automatically by AI Flow: tickets are waiting for a tool that no machine has.\n\n"
                   f"Tool: {requirement}\n"
                   f"Missing since: {since:%Y-%m-%d %H:%M} UTC (over 24 hours; no online machine reports it and no"
                   " project stack declares it)\n"
                   f"Affected projects: {', '.join(projects) or 'none recorded'}\n"
                   f"Machines where it was missing: {', '.join(machines) or 'none recorded'}\n"
                   f"Waiting tickets: {', '.join(w['ticket_key'] for w in waits)}\n\n"
                   f"Next step: install {requirement} on a machine that serves these projects, or add it to a project"
                   " stack in aiflow-control so the node agent prepares it. The waiting tickets are offered again"
                   " automatically when a machine reports the tool.")
    project = getattr(ctx.settings, "maintenance_project", "JAIF")
    try:
        key = ctx.jira.create_issue(project, summary, description, issuetype="Task", labels=[MAINTENANCE_LABEL])
    except Exception as exc:  # noqa: BLE001 - retried by the next sweep
        log.warning("could not open the tooling ticket for %s: %s", requirement, type(exc).__name__)
        return ""
    ctx.db.run("UPDATE tooling_gaps SET maintenance_key=%s, maintenance_opened_at=now() WHERE requirement=%s",
               (key, requirement))
    ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
               " VALUES (%s,%s,'Created','Task',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
               (key, project, [MAINTENANCE_LABEL], summary, description))
    ctx.events.emit("tooling.maintenance_ticket", ticket=key, project=project, outcome="created",
                    reason=f"{requirement} missing for over 24 hours; waiting: " + ", ".join(w["ticket_key"] for w in waits))
    return key


# --- project check commands on a clean checkout ----------------------------------------------------------------
def probe_items(project) -> list[dict[str, Any]]:
    """Repositories of the project with the check commands to run (one entry per configured repository)."""
    items = []
    for repo_name in project.configured_repositories:
        settings = project.settings_for_repo(repo_name)
        checks = [str(c).strip() for c in settings.get("checks") or [] if str(c).strip()]
        if checks:
            items.append({"repo": settings["repo"], "base": settings["base"], "checks": checks})
    return items


def probe_signature(items: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()[:16]


def pending_probes(ctx, node: str, project_keys: list[str]) -> list[dict[str, Any]]:
    """Check-command runs this machine still owes: new project, new machine or changed commands."""
    repo = ctx.repo()
    done = {r["project"]: r["signature"] for r in ctx.db.all(
        "SELECT project, signature FROM project_check_probes WHERE node=%s", (node,))}
    out = []
    for key in project_keys:
        project = repo.projects.get(key)
        if not project or project.paused:
            continue
        items = probe_items(project)
        signature = probe_signature(items)
        if items and done.get(key) != signature:
            out.append({"project": key, "signature": signature, "repositories": items})
    return out


def save_probe(ctx, node: str, project: str, signature: str, results: list[dict[str, Any]]) -> None:
    ctx.db.run("INSERT INTO project_check_probes (project, node, signature, results) VALUES (%s,%s,%s,%s)"
               " ON CONFLICT (project, node) DO UPDATE SET signature=EXCLUDED.signature, checked_at=now(),"
               " results=EXCLUDED.results", (project, node, signature, J(results)))


def probe_report(ctx, project_keys: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Per project and machine: which check commands cannot run (Projects screen)."""
    repo = ctx.repo()
    rows = ctx.db.all("SELECT project, node, signature, checked_at, results FROM project_check_probes ORDER BY node")
    report: dict[str, dict[str, Any]] = {}
    for key, project in repo.projects.items():
        if project_keys is not None and key not in project_keys:
            continue
        items = probe_items(project)
        report[key] = {"has_checks": bool(items), "machines": [], "cannot_run": 0,
                       "signature": probe_signature(items)}
    for r in rows:
        entry = report.get(r["project"])
        if not entry:
            continue
        blocked = [x for x in r["results"] if x.get("status") == "cannot_run"]
        entry["machines"].append({"node": r["node"], "checked_at": r["checked_at"].isoformat(),
                                  "current": r["signature"] == entry["signature"], "cannot_run": blocked,
                                  "ran": sum(1 for x in r["results"] if x.get("status") == "ran")})
        entry["cannot_run"] += len(blocked)
    return report
