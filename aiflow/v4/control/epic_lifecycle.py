"""Keep mirrored epic status aligned with the status of its descendant tickets."""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from urllib.parse import quote

from . import projects as project_lifecycle
from .application_context import ApplicationContext
from .features.tickets import ticket_repository_of

log = logging.getLogger("aiflow4.epic_lifecycle")

FINISHED = frozenset({"Done", "Declined"})
NOT_STARTED = frozenset({"Backlog", "Intake", "To Do"})
EPIC_NOT_STARTED = NOT_STARTED | {"Needs owner"}


def is_under_epic(ticket: dict, epic_key: str, by_key: dict[str, dict]) -> bool:
    """Return whether a ticket's parent chain reaches the given epic."""
    parent = ticket.get("parent_key")
    seen: set[str] = set()
    while parent and parent not in seen:
        if parent == epic_key:
            return True
        seen.add(parent)
        parent_ticket = by_key.get(parent)
        parent = parent_ticket.get("parent_key") if parent_ticket else None
    return False


def children_for(epic: dict, rows: list[dict], by_key: dict[str, dict]) -> list[dict]:
    """Return non-epic descendants, including tickets nested below another child."""
    return [row for row in rows if str(row.get("type") or "").strip().casefold() != "epic"
            and is_under_epic(row, epic["key"], by_key)]


def target_status(epic: dict, children: list[dict], changed: set[str], first_seen: set[str],
                  preexisting: set[str], latest_moves: dict[str, datetime] | None = None) -> str | None:
    """Choose the next epic status from its current status and the complete child set."""
    if not children:
        return None
    if all(child["status"] in FINISHED for child in children):
        return "Done" if epic["status"] != "Done" else None
    changed_reopened = any(
            child["key"] in changed and child["status"] not in FINISHED and
            (child["key"] not in first_seen or epic["key"] in preexisting) and
            (epic.get("done_at") is None or _child_changed_after(child, epic["done_at"], latest_moves))
            for child in children)
    epic_done_at = epic.get("done_at")
    watchdog_reopened = epic_done_at is not None and latest_moves is not None and any(
        child["status"] not in FINISHED and _child_changed_after(child, epic_done_at, latest_moves)
        for child in children)
    if epic["status"] == "Done" and (changed_reopened or watchdog_reopened):
        return "To Do"
    if epic["status"] in EPIC_NOT_STARTED and any(
            child["status"] not in NOT_STARTED and child["status"] != "Declined" and
            (child["key"] not in first_seen or epic["key"] in preexisting) for child in children):
        return "In Progress"
    return None


def _child_changed_after(child: dict, completed_at: datetime,
                         latest_moves: dict[str, datetime] | None) -> bool:
    """Return whether a child status move, parent change or creation happened after the epic closed."""
    return any(value is not None and value > completed_at for value in (
        (latest_moves or {}).get(child["key"]), child.get("parent_changed_at"), child.get("jira_created")))


def transition_reason(target: str, children: list[dict]) -> str:
    """Describe the child condition that caused the selected epic transition."""
    if target == "Done":
        if all(child["status"] == "Done" for child in children):
            return f"all {len(children)} child tickets are Done"
        return f"all {len(children)} child tickets are Done or Declined"
    if target == "In Progress":
        return "the first child ticket started"
    return f"{len(children)} child tickets are not all Done"


def completion_comment(ctx: ApplicationContext, project: str, epic: dict, children: list[dict],
                       epics: list[dict], completed_at: datetime) -> str:
    """Build the comment left when child work completes an epic."""
    done_count = sum(child["status"] == "Done" for child in children)
    declined_count = sum(child["status"] == "Declined" for child in children)
    child_summary = (f"{len(children)} (all Done)" if not declined_count else
                     f"{len(children)} (Done: {done_count}, Declined: {declined_count})")
    report = (ctx.settings.public_url.rstrip("/") + f"/v1/projects/{quote(project, safe='')}/report"
              f"?epic={quote(epic['key'], safe='')}&format=html")
    comment = ("AI Flow · epic completed\n" f"Child tickets: {child_summary}\n"
               f"Completed at: {completed_at.isoformat(timespec='seconds')}\nReport: {report}")
    project_done = bool(epics) and all(row["status"] == "Done" or row["key"] == epic["key"] for row in epics)
    if project_done:
        project_report = (ctx.settings.public_url.rstrip("/") +
                          f"/v1/projects/{quote(project, safe='')}/report?format=html")
        comment += (f"\nProject suggestion: all {len(epics)} epics are Done; "
                    f"consider closing project {project}. Report: {project_report}")
    return comment


def apply_transition(ctx: ApplicationContext, project: str, epic: dict, children: list[dict],
                     epics: list[dict], target: str, source: str) -> None:
    """Move one epic and record its mirror, timeline, audit and completion comment."""
    key = epic["key"]
    previous = epic["status"]
    detail = transition_reason(target, children)
    try:
        current = ctx.jira.status(key)
        if target == "In Progress" and current in FINISHED:
            return
        moved = current != target
        status = ctx.jira.transition_to(key, target) if moved else current
        if status != target:
            return
        now = datetime.now(timezone.utc)
        ticket_repository_of(ctx).update_status_with_done_timestamp((status, status, now, key))
        epic["status"] = status
        ready = "aiflow-ready" in (epic.get("labels") or [])
        ctx.events.emit("ticket.status", ticket=key, project=project, outcome=status,
                        reason=f"from {previous}", from_status=previous, ready=ready, source=source)
        if target == "Done":
            try:
                project_lifecycle.report_done_epic(ctx, project, key)
            except Exception as error:  # noqa: BLE001 - reporting must not undo the completed epic transition
                log.warning("could not generate completion report for epic %s: %s", key, error)
        if not moved:
            return
        ctx.events.emit("jira.transition", ticket=key, account="control", outcome=status, reason=detail)
        from . import merge_flow
        merge_flow.record_move(ctx, key, previous, status, merge_flow.EPIC_CHILD, detail)
        ticket_repository_of(ctx).record_epic_transition(key, status, source, detail, len(children))
        if target == "Done":
            ctx.jira.comment(key, completion_comment(ctx, project, epic, children, epics, now))
    except Exception as error:  # noqa: BLE001 - epic follow-up must not fail the accepted child transition; watchdog retries
        log.warning("could not reconcile epic %s: %s", key, error)


def reconcile(ctx: ApplicationContext, projects: list[str], changed: set[str], first_seen: set[str],
              preexisting: set[str], source: str = "jira_sync",
              latest_moves: dict[str, datetime] | None = None) -> None:
    """Apply any child-driven status change for each epic in the named projects."""
    if not ctx.jira:
        return
    repository = ticket_repository_of(ctx)
    if latest_moves is None:
        latest_moves = {
            move.ticket: move.ts
            for project in projects
            for move in repository.latest_project_ticket_moves(project)
        } if hasattr(repository, "latest_project_ticket_moves") else {}
    for project in projects:
        rows = [asdict(row) if not isinstance(row, dict) else row
                for row in repository.list_project_tickets_for_epic_reconcile((project,))]
        by_key = {row["key"]: row for row in rows}
        epics = [row for row in rows if row.get("project") == project and
                 str(row.get("type") or "").strip().casefold() == "epic"]
        for epic in epics:
            children = children_for(epic, rows, by_key)
            target = target_status(epic, children, changed, first_seen, preexisting, latest_moves)
            if target:
                apply_transition(ctx, project, epic, children, epics, target, source)


def reconcile_from_watchdog(ctx: ApplicationContext) -> None:
    """Retry failed child-start, completion and recently reopened epic transitions."""
    if not ctx.jira:
        return
    projects = [key for key, project in ctx.repo().projects.items() if not project.paused]
    repository = ticket_repository_of(ctx)
    latest_moves = {
        row.ticket: row.ts
        for project in projects
        for row in repository.latest_project_ticket_moves(project)
    }
    reconcile(ctx, sorted(projects), set(), set(), set(), "watchdog", latest_moves)
