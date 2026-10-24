"""Tickets carry requirements only; servers, hosts and access live in the project configuration (docs/v4/environment.md).

The control-plane side of the rule:

* missing environment items: a developer run that needs an item that is not provided reports its name; the report is
  recorded by name, the ticket waits (it is not offered again and not a question to the owner) until the item is
  provided or its configuration changes, and one environment ticket is raised per project and item;
* the record of what the intake AI removed from a ticket, and the environment ticket it opens or links for a real
  need behind the removed wording;
* the scan of all open tickets of every project for server wording, with the automatic correction and the report.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.environment import find_server_wording, kinds_of, redact
from .db import J

log = logging.getLogger("aiflow4.environment")
ENVIRONMENT_LABEL = "environment"
TICKET_AFTER = timedelta(hours=1)        # an item still missing after this long gets one environment ticket
SCAN_EVERY = timedelta(hours=24)
CLOSED_STATUSES = ("Done", "Closed", "Declined", "Cancelled", "Canceled", "Resolved", "Won't Do")
# only a ticket nobody is working on is sent back to intake; work in flight is reported and left alone
CORRECTABLE_STATUSES = ("Created", "Backlog", "To Do", "Needs owner")


class UnmappedPostMergeCriteria(ValueError):
    """A verify PASS cannot defer a criterion without an exact configured check."""


def _item(ctx, project: str, name: str):
    p = ctx.repo().projects.get(project)
    return next((i for i in (p.environment_items if p else []) if i.name == name), None)


# --- missing environment items ------------------------------------------------------------------------------------
def record_missing(ctx, run: dict, names: list[str], detail: str) -> list[str]:
    """Record the items a developer run reported missing. Only names of the project's configuration are accepted."""
    recorded: list[str] = []
    for name in dict.fromkeys(str(n).strip().upper() for n in names if str(n).strip()):
        item = _item(ctx, run["project"], name)
        if not item:
            continue
        ctx.db.run("INSERT INTO environment_reports (ticket_key, project, item, machine, run_id, detail, fingerprint)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (ticket_key, item) WHERE resolved_at IS NULL"
                   " DO UPDATE SET machine=EXCLUDED.machine, run_id=EXCLUDED.run_id, detail=EXCLUDED.detail",
                   (run["ticket_key"], run["project"], name, run["node"], str(run["id"]), detail[:1000],
                    item.fingerprint()))
        recorded.append(name)
    if recorded:
        ctx.events.emit("ticket.waiting_for_environment", ticket=run["ticket_key"], project=run["project"],
                        run_id=str(run["id"]), node=run["node"], outcome="waiting",
                        reason="missing environment item: " + ", ".join(recorded))
    return recorded


def parked_tickets(ctx) -> set[str]:
    """Tickets waiting for an environment item; they are not offered again until it is provided."""
    return {r["ticket_key"] for r in ctx.db.all("SELECT ticket_key FROM environment_reports WHERE resolved_at IS NULL")}


def provide(ctx, project: str, item: str, how: str = "provided", *, machine: str = "",
            fingerprint: str = "") -> int:
    """Confirm an item and offer its waiting tickets again.

    Node confirmations are bound to the reporting node and the item's current configuration fingerprint, so a
    delayed heartbeat cannot resolve a newer wait or a wait on a different node.
    """
    rows = ctx.db.all("UPDATE environment_reports SET resolved_at=now(), resolution=%s WHERE project=%s AND item=%s"
                      " AND resolved_at IS NULL AND (%s='' OR machine=%s) AND (%s='' OR fingerprint=%s)"
                      " RETURNING ticket_key", (how[:200], project, item, machine, machine, fingerprint, fingerprint))
    for row in rows:
        ctx.events.emit("ticket.environment_provided", ticket=row["ticket_key"], project=project, outcome="provided",
                        reason=f"{item}: {how}")
    return len(rows)


def confirm_node_environment(ctx, node: str, reported: dict[str, list[str]]) -> int:
    """Resolve waits from the node agent's authenticated, names-only report of configured secrets it can read."""
    repo = ctx.repo()
    node_row = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name=%s AND removed_at IS NULL", (node,)) or {}
    assigned = set(repo.projects_for_node(node, node_row.get("projects") or [], node_row.get("dynamic", False)))
    resolved = 0
    for project in sorted(assigned.intersection(reported)):
        configured = {item.name: item for item in repo.projects[project].environment_items if item.secret}
        for name in dict.fromkeys(str(value).strip().upper() for value in reported.get(project, []) if str(value).strip()):
            item = configured.get(name)
            if item:
                resolved += provide(ctx, project, name, "secret confirmed by the node agent", machine=node,
                                    fingerprint=item.fingerprint())
    return resolved


def sweep(ctx) -> list[str]:
    """Close the waits that ended and raise the one environment ticket per project and item. Returns the keys opened."""
    for r in ctx.db.all("SELECT id, ticket_key, project, item, fingerprint FROM environment_reports WHERE resolved_at IS NULL"):
        item = _item(ctx, r["project"], r["item"])
        why = ""
        if not item:
            why = "the item is no longer in the project configuration"
        elif item.fingerprint() != r["fingerprint"]:
            why = "the configuration of the item changed"
        if why:
            ctx.db.run("UPDATE environment_reports SET resolved_at=now(), resolution=%s WHERE id=%s", (why, r["id"]))
    ctx.db.run("UPDATE environment_reports SET resolved_at=now(), resolution='ticket is done' WHERE resolved_at IS NULL"
               " AND ticket_key IN (SELECT key FROM tickets WHERE status='Done')")
    opened: list[str] = []
    groups = ctx.db.all("SELECT project, item, min(created_at) AS since, array_agg(ticket_key ORDER BY ticket_key) AS tickets,"
                        " max(environment_ticket) AS environment_ticket FROM environment_reports"
                        " WHERE resolved_at IS NULL GROUP BY project, item")
    for g in groups:
        if g["environment_ticket"] or datetime.now(timezone.utc) - g["since"] < TICKET_AFTER or not ctx.jira:
            continue
        key = open_environment_ticket(ctx, g["project"], g["item"], g["tickets"])
        if key:
            opened.append(key)
    opened.extend(retry_pending_needs(ctx))
    return opened


def open_environment_ticket(ctx, project: str, item: str, tickets: list[str]) -> str:
    summary = f"[Environment] Provide {item} for {project} runs"
    description = ("Business requirement: Runs of the project need an environment item that is configured but is not "
                   "provided to them, so tickets wait. AI Flow gives a run its environment; this ticket makes the "
                   "item available.\n\n"
                   "Acceptance criteria:\n"
                   f"- AC1: {item} is provided to the runs of {project} that the project configuration lets use it.\n"
                   "- AC2: The tickets that waited for it are offered again and their runs find the variable set.\n\n"
                   "Technical notes: Opened automatically by AI Flow. The item is described in the environment section "
                   f"of the project configuration. Waiting tickets: {', '.join(tickets)}.\n\n"
                   "Definition of done:\n- A run of the project reads the variable; the waiting tickets continue.")
    project_key = getattr(ctx.settings, "maintenance_project", "JAIF")
    try:
        key = ctx.jira.create_issue(project_key, summary, description, issuetype="Task", labels=[ENVIRONMENT_LABEL])
    except Exception as exc:  # noqa: BLE001 - retried by the next sweep
        log.warning("could not open the environment ticket for %s: %s", item, type(exc).__name__)
        return ""
    ctx.db.run("UPDATE environment_reports SET environment_ticket=%s WHERE project=%s AND item=%s AND resolved_at IS NULL",
               (key, project, item))
    ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
               " VALUES (%s,%s,'Created','Task',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
               (key, project_key, [ENVIRONMENT_LABEL], summary, description))
    ctx.events.emit("environment.ticket", ticket=key, project=project_key, outcome="created",
                    reason=f"{item} missing for {project}; waiting: " + ", ".join(tickets))
    return key


def reports(ctx, limit: int = 200) -> list[dict[str, Any]]:
    """Missing environment items for the console: by name, with the waiting ticket and the environment ticket."""
    rows = ctx.db.all("SELECT r.ticket_key, r.project, r.item, r.machine, r.detail, r.environment_ticket, r.created_at,"
                      " r.resolved_at, r.resolution, t.summary FROM environment_reports r"
                      " LEFT JOIN tickets t ON t.key = r.ticket_key"
                      " ORDER BY (r.resolved_at IS NOT NULL), r.created_at DESC LIMIT %s", (limit,))
    return [{"ticket": r["ticket_key"], "summary": r["summary"] or "", "project": r["project"], "item": r["item"],
             "machine": r["machine"], "detail": r["detail"], "environment_ticket": r["environment_ticket"],
             "since": r["created_at"].isoformat(), "state": "provided" if r["resolved_at"] else "waiting",
             "resolution": r["resolution"]} for r in rows]


# --- what the intake AI removed -----------------------------------------------------------------------------------
def need_key(need: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", need.lower()))[:160]


def record_removal(ctx, run: dict, removed: list[str], needs: list[str]) -> None:
    ctx.db.run("INSERT INTO environment_removals (ticket_key, project, run_id, removed, needs) VALUES (%s,%s,%s,%s,%s)",
               (run["ticket_key"], run["project"], str(run["id"]), J(removed), J(needs)))
    ctx.events.emit("ticket.server_wording_removed", ticket=run["ticket_key"], project=run["project"],
                    run_id=str(run["id"]), outcome="removed",
                    reason=f"{len(removed)} removed, {len(needs)} environment need(s)")


def record_post_merge_criteria(ctx, run: dict, criteria: list[Any]) -> list[dict[str, str]]:
    """Save criteria the reviewer deferred, each tied to one configured check command."""
    if run.get("step") != "verify":
        return []
    project = ctx.repo().projects.get(run["project"])
    settings = project.settings_for_repo(str((run.get("details") or {}).get("repository") or project.repo)) if project else {}
    configured_checks = set(settings.get("checks") or (project.checks if project else []))
    clean: list[dict[str, str]] = []
    for raw in criteria[:20]:
        if isinstance(raw, dict):
            criterion_id = str(raw.get("id") or "").strip().upper()[:24]
            text = str(raw.get("text") or "").strip()
            check = str(raw.get("check") or "").strip()
        else:
            criterion_id, text, check = "", str(raw).strip(), ""
        if not text:
            continue
        text = redact(text)[:600]
        clean.append({"id": criterion_id, "text": text,
                      "check": check if check in configured_checks else ""})
    clean = list({(item["id"], item["text"]): item for item in clean}.values())
    if not clean:
        ctx.db.run("DELETE FROM environment_post_merge_checks WHERE ticket_key=%s AND status='pending'",
                   (run["ticket_key"],))
        return []
    unmapped = [item for item in clean if not item["check"]]
    if unmapped:
        ids = ", ".join(item["id"] or "deferred criterion" for item in unmapped[:8])
        raise UnmappedPostMergeCriteria(
            f"{ids} must map to a valid configured post-merge check before verify can pass")
    ctx.db.run("INSERT INTO environment_post_merge_checks (ticket_key, project, criteria, status, result, release_ticket, run_id, checked_at)"
               " VALUES (%s,%s,%s,'pending','{}','',%s,NULL) ON CONFLICT (ticket_key) DO UPDATE SET"
               " project=EXCLUDED.project, criteria=EXCLUDED.criteria, status='pending', result='{}',"
               " release_ticket='', run_id=EXCLUDED.run_id, updated_at=now(), checked_at=NULL",
               (run["ticket_key"], run["project"], J(clean), str(run["id"])))
    ctx.events.emit("environment.post_merge_queued", ticket=run["ticket_key"], project=run["project"],
                    step="verify", run_id=str(run["id"]), outcome="pending",
                    reason=f"{len(clean)} criterion/criteria queued for post-merge verification")
    return clean


def pending_post_merge(ctx, project: str, ticket_keys: list[str] | None = None) -> list[dict[str, Any]]:
    """Deferred criteria for merged tickets, whether or not they belong to a Jira version release."""
    if ticket_keys is not None and not ticket_keys:
        return []
    if ticket_keys is None:
        rows = ctx.db.all("SELECT c.ticket_key, c.criteria FROM environment_post_merge_checks c"
                          " JOIN tickets t ON t.key=c.ticket_key WHERE c.project=%s AND c.status='pending'"
                          " AND t.status='Done' ORDER BY c.created_at, c.ticket_key", (project,))
    else:
        rows = ctx.db.all("SELECT c.ticket_key, c.criteria FROM environment_post_merge_checks c"
                          " JOIN tickets t ON t.key=c.ticket_key WHERE c.project=%s AND c.status='pending'"
                          " AND c.ticket_key = ANY(%s) AND t.status='Done' ORDER BY c.created_at, c.ticket_key",
                          (project, ticket_keys))
    return [{"ticket": row["ticket_key"], "criteria": row["criteria"] or []} for row in rows]


def record_post_merge_results(ctx, run: dict, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Record each deferred criterion only from the check command explicitly mapped to it."""
    checks = {str(item.get("command") or ""): item for item in report.get("checks") or []}
    recorded: list[dict[str, Any]] = []
    ticket = str(run.get("ticket_key") or "")
    row = ctx.db.one("SELECT criteria,project FROM environment_post_merge_checks WHERE ticket_key=%s"
                     " AND status='pending'", (ticket,))
    if not row:
        return recorded
    results = []
    for criterion in row["criteria"] or []:
        command = str(criterion.get("check") or "") if isinstance(criterion, dict) else ""
        check = checks.get(command) if command else None
        status = ("passed" if bool(check.get("passed")) else "failed") if check else "failed"
        result = {"id": criterion.get("id", "") if isinstance(criterion, dict) else "",
                  "text": criterion.get("text", "") if isinstance(criterion, dict) else str(criterion),
                  "check": command, "status": status,
                  "check_result": {"command": command, "passed": bool(check.get("passed")),
                                   "exit_code": check.get("exit_code")} if check else None}
        results.append(result)
    status = "passed" if results and all(item["status"] == "passed" for item in results) else "failed"
    result = {"run_id": str(run["id"]), "head_sha": str(report.get("head_sha") or ""),
              "criteria": results, "error": str(report.get("error") or "")[:1000]}
    count = ctx.db.run("UPDATE environment_post_merge_checks SET status=%s,result=%s,release_ticket=%s,run_id=%s,"
                       "checked_at=now(),updated_at=now() WHERE ticket_key=%s AND project=%s AND status='pending'",
                       (status, J(result), ticket, str(run["id"]), ticket, row["project"]))
    if count:
        ctx.events.emit("environment.post_merge_result", ticket=ticket, project=row["project"], step="post_merge",
                        run_id=str(run["id"]), outcome=status, reason="per-criterion post-merge checks",
                        data=result)
        recorded.append({"ticket": ticket, "status": status, "criteria": results})
    return recorded


def post_merge_report(ctx, project: str) -> list[dict[str, Any]]:
    rows = ctx.db.all("SELECT ticket_key, criteria, status, result, release_ticket, updated_at, checked_at"
                      " FROM environment_post_merge_checks WHERE project=%s ORDER BY updated_at DESC LIMIT 100",
                      (project,))
    return [{"ticket": row["ticket_key"], "criteria": row["criteria"] or [], "status": row["status"],
             "result": row["result"] or {}, "release_ticket": row["release_ticket"],
             "updated_at": row["updated_at"].isoformat(),
             "checked_at": row["checked_at"].isoformat() if row["checked_at"] else None} for row in rows]


def open_need_tickets(ctx, run: dict, needs: list[str]) -> list[dict[str, Any]]:
    """Durably record each real need, then open or link its environment ticket."""
    out: list[dict[str, Any]] = []
    for need in dict.fromkeys(n.strip() for n in needs if n and n.strip()):
        need = redact(need)[:300]
        nk = need_key(need)
        if not nk:
            continue
        ctx.db.run("INSERT INTO environment_needs (project, need_key, need, environment_ticket, linked_tickets)"
                   " VALUES (%s,%s,%s,'',%s) ON CONFLICT (project, need_key) DO UPDATE SET"
                   " linked_tickets=CASE WHEN %s = ANY(environment_needs.linked_tickets)"
                   " THEN environment_needs.linked_tickets ELSE array_append(environment_needs.linked_tickets,%s) END",
                   (run["project"], nk, need, [run["ticket_key"]], run["ticket_key"], run["ticket_key"]))
        row = ctx.db.one("SELECT environment_ticket FROM environment_needs WHERE project=%s AND need_key=%s",
                         (run["project"], nk))
        key = str((row or {}).get("environment_ticket") or "")
        created = False
        if not key:
            key, created = _create_pending_need_ticket(ctx, run["project"], nk)
        elif ctx.jira:
            _link_need_ticket(ctx, key, [run["ticket_key"]])
        if key:
            ctx.events.emit("environment.need", ticket=run["ticket_key"], project=run["project"],
                            outcome="created" if created else "linked", reason=f"{key}: {need}")
        out.append({"need": need, "ticket": key, "created": created, "pending": not bool(key)})
    return out


def _link_need_ticket(ctx, key: str, tickets: list[str]) -> None:
    for ticket in dict.fromkeys(tickets):
        if not ctx.jira:
            return
        try:
            ctx.jira.link_issues(key, ticket, "Relates")
        except Exception as exc:  # noqa: BLE001 - linked tickets remain in the durable need record
            log.warning("could not link %s to %s: %s", key, ticket, type(exc).__name__)


def _create_pending_need_ticket(ctx, project: str, nk: str) -> tuple[str, bool]:
    """Try Jira creation for a need that is already durably stored in environment_needs."""
    row = ctx.db.one("SELECT need, environment_ticket, linked_tickets FROM environment_needs"
                     " WHERE project=%s AND need_key=%s", (project, nk))
    if not row or row["environment_ticket"] or not ctx.jira:
        return str((row or {}).get("environment_ticket") or ""), False
    project_key = getattr(ctx.settings, "maintenance_project", "JAIF")
    need = str(row["need"])
    summary = f"[Environment] {need}"[:240]
    description = (f"Business requirement: {need}. A story of project {project} needs this from the environment of its "
                   "runs; the story states the requirement only, so the environment feature is tracked here.\n\n"
                   "Acceptance criteria:\n"
                   "- AC1: The runs of the project get what is needed, through the environment section of the project "
                   "configuration, as environment variables.\n\n"
                   "Technical notes: Opened automatically by AI Flow from a story that named where and how this runs; "
                   "that wording was removed from the story.\n\n"
                   "Definition of done:\n- The environment section lists the item and a run receives it.")
    try:
        key = ctx.jira.create_issue(project_key, summary, description, issuetype="Task", labels=[ENVIRONMENT_LABEL])
    except Exception as exc:  # noqa: BLE001 - the next environment sweep retries this pending need
        log.warning("could not open the environment ticket for %s: %s", project, type(exc).__name__)
        return "", False
    ctx.db.run("UPDATE environment_needs SET environment_ticket=%s WHERE project=%s AND need_key=%s"
               " AND environment_ticket=''", (key, project, nk))
    ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
               " VALUES (%s,%s,'Created','Task',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
               (key, project_key, [ENVIRONMENT_LABEL], summary, description))
    _link_need_ticket(ctx, key, row["linked_tickets"] or [])
    ctx.events.emit("environment.need_ticket", ticket=key, project=project_key, outcome="created",
                    reason=f"{need}; requested by " + ", ".join(row["linked_tickets"] or []))
    return key, True


def retry_pending_needs(ctx) -> list[str]:
    """Retry environment-ticket creation after Jira was temporarily unavailable during intake."""
    opened = []
    for row in ctx.db.all("SELECT project, need_key FROM environment_needs WHERE environment_ticket=''"
                          " ORDER BY created_at"):
        key, created = _create_pending_need_ticket(ctx, row["project"], row["need_key"])
        if key and created:
            opened.append(key)
    return opened


# --- the scan of the open tickets ---------------------------------------------------------------------------------
def scan(ctx, actor: str = "system", correct: bool = True) -> dict[str, Any]:
    """List the open tickets of every project that name a host, machine, network or access instruction, send the
    ones nobody works on back to intake (the intake AI removes the wording and keeps the requirement), and keep
    the report. The report never repeats the wording; it names the kinds."""
    rows = ctx.db.all("SELECT key, project, status, labels, summary, description FROM tickets"
                      " WHERE NOT (status = ANY(%s)) ORDER BY project, key", (list(CLOSED_STATUSES),))
    offenders: list[dict[str, Any]] = []
    for t in rows:
        findings = find_server_wording(f"{t['summary']}\n{t['description']}")
        if not findings:
            continue
        entry: dict[str, Any] = {"ticket": t["key"], "project": t["project"], "status": t["status"],
                                 "kinds": kinds_of(findings), "count": len(findings), "action": "reported"}
        working = ctx.db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND status='running'", (t["key"],))
        if t["status"] == "Intake":
            entry["action"] = "already in intake"
        elif correct and ctx.jira and t["status"] in CORRECTABLE_STATUSES and not working:
            entry["action"] = _send_to_intake(ctx, t, entry["kinds"])
        else:
            entry["action"] = "work in flight; left as it is" if t["status"] not in CORRECTABLE_STATUSES or working \
                else "reported"
        offenders.append(entry)
    saved = ctx.db.one("INSERT INTO environment_scans (actor, scanned, offenders) VALUES (%s,%s,%s)"
                       " RETURNING id, created_at", (actor, len(rows), J(offenders)))
    ctx.events.emit("environment.scan", outcome="done", reason=f"{len(offenders)} of {len(rows)} open tickets name server wording")
    return {"id": saved["id"], "created_at": saved["created_at"].isoformat(), "actor": actor, "scanned": len(rows),
            "offenders": offenders}


def _send_to_intake(ctx, t: dict, kinds: list[str]) -> str:
    key = t["key"]
    try:
        ctx.jira.comment(key, "AI Flow · environment scan · sent back to intake\n"
                              f"Why: the ticket names {', '.join(kinds)}. A ticket carries the requirement only; where and how "
                              "something runs belongs to the project configuration and the run environment.\n"
                              "Next: the intake AI removes that wording, keeps the requirement and records what it removed.")
        ctx.jira.remove_label(key, "aiflow-ready")
        status = ctx.jira.transition_to(key, "Intake")
    except Exception as exc:  # noqa: BLE001 - reported, retried by the next scan
        log.warning("could not send %s back to intake: %s", key, type(exc).__name__)
        return "could not be moved; reported"
    ctx.db.run("UPDATE tickets SET status=%s, labels=array_remove(labels,'aiflow-ready'), synced_at=now() WHERE key=%s",
               (status, key))
    return "sent back to intake"


def last_scan(ctx) -> dict[str, Any] | None:
    row = ctx.db.one("SELECT id, created_at, actor, scanned, offenders FROM environment_scans ORDER BY id DESC LIMIT 1")
    if not row:
        return None
    return {"id": row["id"], "created_at": row["created_at"].isoformat(), "actor": row["actor"],
            "scanned": row["scanned"], "offenders": row["offenders"]}


def scan_if_due(ctx) -> dict[str, Any] | None:
    """The loops run the scan once a day."""
    row = ctx.db.one("SELECT max(created_at) AS at FROM environment_scans")
    if row and row["at"] and datetime.now(timezone.utc) - row["at"] < SCAN_EVERY:
        return None
    return scan(ctx)
