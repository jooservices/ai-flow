"""Project close reports, versioned study cases, and automatic reopen on new work."""
from __future__ import annotations

import logging
from typing import Any

from . import notify
from .db import J
from .report import build_report, render_markdown

log = logging.getLogger("aiflow4.projects")


def _save_report(ctx, project: str, scope: str, subject: str, actor: str) -> dict[str, Any]:
    """Persist one report and its memory entry atomically; callers handle notification after commit."""
    with ctx.db.pool.connection() as c:
        with c.transaction():
            c.execute("INSERT INTO project_lifecycle (project) VALUES (%s) ON CONFLICT (project) DO NOTHING",
                      (project,))
            lifecycle = c.execute("SELECT status, version FROM project_lifecycle WHERE project=%s FOR UPDATE",
                                  (project,)).fetchone()
            if scope == "project" and lifecycle["status"] == "closed":
                prior = c.execute("SELECT id, version, report, markdown, created_at FROM project_reports"
                                  " WHERE project=%s AND scope='project' AND subject=%s ORDER BY version DESC LIMIT 1",
                                  (project, subject)).fetchone()
                if prior:
                    return {**prior, "created": False, "study_case_ref": f"{project}:v{prior['version']}"}

            if scope == "project":
                version = int(lifecycle["version"]) + 1
            else:
                version = int(c.execute("SELECT coalesce(max(version), 0) + 1 AS version FROM project_reports"
                                        " WHERE project=%s AND scope='epic' AND subject=%s",
                                        (project, subject)).fetchone()["version"])

            report = build_report(ctx, project, subject if scope == "epic" else None)
            markdown = render_markdown(report)
            ref = f"{project}:v{version}" if scope == "project" else f"{project}:epic:{subject}:v{version}"
            stored = c.execute("INSERT INTO project_reports (project, scope, subject, version, report, markdown)"
                                " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, created_at",
                                (project, scope, subject, version, J(report), markdown)).fetchone()
            c.execute("INSERT INTO memory (project, kind, ref, ticket, text, created_at)"
                      " VALUES (%s,'study_case',%s,'',%s,%s)",
                      (project, ref, markdown, stored["created_at"]))
            if scope == "project":
                c.execute("UPDATE project_lifecycle SET status='closed', closed_at=now(), closed_by=%s,"
                          " version=%s, updated_at=now() WHERE project=%s", (actor, version, project))
            return {"id": stored["id"], "version": version, "report": report, "markdown": markdown,
                    "created_at": stored["created_at"], "created": True, "study_case_ref": ref}


def _deliver(ctx, project: str, scope: str, subject: str, result: dict[str, Any]) -> list[str]:
    if not result["created"]:
        return []
    label = project if scope == "project" else f"{project} · epic {subject}"
    title = f"{label} closed — study case v{result['version']}" if scope == "project" else \
        f"{label} completed — study case v{result['version']}"
    try:
        sent = notify.send("info", title, result["markdown"], "/knowledge", ctx=ctx,
                           alert_kind="daily_summary", project=project, ticket=f"{project} project report",
                           needed="Review the completed project report and study case.")
    except Exception as exc:  # noqa: BLE001
        log.warning("project report notification failed for %s: %s", project, exc)
        sent = []
    ctx.db.run("UPDATE project_reports SET sent_channels=%s WHERE id=%s", (sent, result["id"]))
    ctx.events.emit("project.report_generated", project=project, outcome=scope,
                    reason=f"{result['study_case_ref']} sent via {', '.join(sent) or 'no configured channel'}")
    return sent


def close_project(ctx, project: str, actor: str = "owner") -> dict[str, Any]:
    """Close once, generate a versioned report, index it as a study case, and notify configured channels."""
    actor = (actor or "owner").strip()[:120] or "owner"
    result = _save_report(ctx, project, "project", project, actor)
    sent = _deliver(ctx, project, "project", project, result)
    result["sent_channels"] = sent
    result["status"] = "closed"
    return result


def report_done_epic(ctx, project: str, epic: str) -> dict[str, Any]:
    """Create a study case when Jira first reports an epic as Done."""
    result = _save_report(ctx, project, "epic", epic, "system")
    result["sent_channels"] = _deliver(ctx, project, "epic", epic, result)
    return result


def reopen_for_new_ticket(ctx, project: str, ticket: str) -> bool:
    """A newly observed ticket reopens a closed project and preserves its report version history."""
    row = ctx.db.one("UPDATE project_lifecycle SET status='open', closed_at=NULL, closed_by='', updated_at=now()"
                     " WHERE project=%s AND status='closed' RETURNING version", (project,))
    if not row:
        return False
    ctx.events.emit("project.reopened", ticket=ticket, project=project, outcome="open",
                    reason="new ticket added after project closure")
    return True


def study_cases(db, project: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """Return complete report text for the Knowledge screen and other memory consumers."""
    limit = max(1, min(int(limit), 100))
    rows = db.all("SELECT project, ref, text, created_at FROM memory WHERE kind='study_case'"
                  " AND (%s='' OR project=%s) ORDER BY created_at DESC, id DESC LIMIT %s",
                  (project or "", project or "", limit))
    return [{"project": row["project"], "ref": row["ref"], "text": row["text"],
             "created_at": row["created_at"].isoformat()} for row in rows]
