"""Business view (/business): change requests of one project and their impact assessment.

Epic progress and the "needs your answer" list come from /v1/state like the rest of the console; this module adds
only what the console does not have yet:

  create   a change request from the Business form: one Jira Story in the project, label change-request, the chosen
           epic as parent, status Backlog. Written through the Jira gateway and recorded in write_audit.
  list     the project's change requests (tickets labelled change-request) with the latest impact assessment of an
           intake refiner (runs of step "impact", outcome "assessed", details.impact) or "assessing" while none.
  readiness  whether each unreleased Jira version of the project is ready to release and why not
           (release_readiness; /v1/state carries the same list for the console).

Neither path moves a ticket out of Backlog: the owner moves an assessed change request to Intake in Jira.
"""
from __future__ import annotations

from typing import Any

from . import release_readiness as readiness
from .projects import reopen_for_new_ticket

LABEL = "change-request"
ACTOR = "business-console"


class Invalid(ValueError):
    """The change request form is incomplete or names an epic outside the project."""


def epics(db, project: str) -> list[dict]:
    return db.all("SELECT key, summary, status FROM tickets WHERE project=%s AND type='Epic' ORDER BY key", (project,))


def change_requests(db, project: str, jira_site: str = "") -> list[dict[str, Any]]:
    rows = db.all("SELECT key, status, summary, parent_key, jira_created FROM tickets"
                  " WHERE project=%s AND %s = ANY(labels) ORDER BY jira_created DESC NULLS LAST, key DESC",
                  (project, LABEL))
    assessed = {r["ticket_key"]: r for r in db.all(
        "SELECT DISTINCT ON (ticket_key) ticket_key, details, ended_at FROM runs"
        " WHERE project=%s AND step='impact' AND outcome='assessed' ORDER BY ticket_key, started_at DESC", (project,))}
    out = []
    for t in rows:
        a = assessed.get(t["key"])
        impact = (a["details"] or {}).get("impact") if a else None
        out.append({"key": t["key"], "summary": t["summary"], "status": t["status"], "epic": t["parent_key"],
                    "url": f"{jira_site.rstrip('/')}/browse/{t['key']}",
                    "assessment": "done" if impact else "assessing", "impact": impact or None,
                    "assessed_at": a["ended_at"].isoformat() if impact and a["ended_at"] else None})
    return out


def release_readiness(db, project: str) -> list[dict[str, Any]]:
    return readiness.project_versions(db, project)


def validate(db, project: str, title: str, description: str, epic: str) -> tuple[str, str, str]:
    title, description, epic = title.strip(), description.strip(), epic.strip()
    missing = [name for name, v in (("title", title), ("description", description)) if not v]
    if missing:
        raise Invalid(" and ".join(missing) + " required")
    if epic and not db.one("SELECT key FROM tickets WHERE key=%s AND project=%s AND type='Epic'", (epic, project)):
        raise Invalid(f"{epic} is not an epic of project {project}")
    return title, description, epic


def create(ctx, project: str, title: str, description: str, epic: str) -> dict[str, Any]:
    """Create the Story in Jira (Backlog), mirror it in the registry and return its key and link."""
    key = ctx.jira.create_issue(project, title, description, issuetype="Story", labels=[LABEL], parent=epic)
    status = ctx.jira.status(key)
    if status != "Backlog":  # projects whose workflow starts elsewhere: a change request waits in Backlog
        status = ctx.jira.transition_to(key, "Backlog")
    ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, parent_key, jira_created,"
               " jira_updated) VALUES (%s,%s,%s,'Story',%s,%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
               " status=EXCLUDED.status, labels=EXCLUDED.labels, parent_key=EXCLUDED.parent_key, synced_at=now()",
               (key, project, status, [LABEL], title, description, epic or None))
    reopen_for_new_ticket(ctx, project, key)
    return {"key": key, "status": status, "epic": epic or None, "url": f"{ctx.settings.jira_site.rstrip('/')}/browse/{key}"}
