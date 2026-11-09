"""Keep mirrored epic status aligned with the status of its descendant tickets."""
from __future__ import annotations

import logging
from contextlib import nullcontext
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
            (epic.get("done_at") is None or _child_changed_after(child, epic["done_at"], latest_moves) or
             (child["key"] in first_seen and epic["key"] in preexisting))
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
                     epics: list[dict], target: str, source: str, changed: set[str] | None = None,
                     first_seen: set[str] | None = None, preexisting: set[str] | None = None,
                     latest_moves: dict[str, datetime] | None = None) -> None:
    """Move one epic from a locked, refreshed child snapshot and persist follow-up work."""
    key = epic["key"]
    repository = ticket_repository_of(ctx, fallback=bool(getattr(ctx, "db", None)))
    changed, first_seen, preexisting = changed or set(), first_seen or set(), preexisting or set()
    transaction = ctx.db.transaction() if getattr(ctx, "db", None) else nullcontext()
    try:
        with transaction:
            if hasattr(repository, "lock_epic_lifecycle"):
                repository.lock_epic_lifecycle(key)
            current_epic, children, epics = _refresh_epic_snapshot(
                repository, project, key, changed, first_seen, preexisting, latest_moves, epic, children, epics)
            if current_epic is None:
                return
            live_target = target_status(current_epic, children, changed, first_seen, preexisting,
                                        _latest_moves(repository, project, latest_moves))
            if live_target != target:
                if hasattr(repository, "clear_epic_reopen_requests"):
                    repository.clear_epic_reopen_requests(key)
                return
            now = datetime.now(timezone.utc)
            if target == "To Do" and hasattr(repository, "queue_epic_reopen_request"):
                for child in children:
                    if child["status"] not in FINISHED:
                        repository.queue_epic_reopen_request((key, child["key"], project, now))
            try:
                current = ctx.jira.status(key)
            except Exception as error:  # noqa: BLE001 - the saved request lets the watchdog retry a failed status read
                log.warning("could not reconcile epic %s: %s", key, error)
                return
            if target == "In Progress" and current in FINISHED:
                return
            # Jira may have accepted a prior attempt before the local follow-up transaction failed.
            moved = current != target or current_epic["status"] != target
            try:
                status = ctx.jira.transition_to(key, target) if moved else current
            except Exception as error:  # noqa: BLE001 - a saved reopen request lets the watchdog retry this move
                log.warning("could not reconcile epic %s: %s", key, error)
                return
            if status != target:
                return
            previous = current_epic["status"]
            detail = transition_reason(target, children)
            ready = "aiflow-ready" in (current_epic.get("labels") or [])
            comment = (completion_comment(ctx, project, current_epic, children, epics, now)
                       if target == "Done" and moved else "")
            repository.queue_epic_transition_followup({
                "ticket": key, "project": project, "from_status": previous, "to_status": status,
                "source": source, "reason": detail, "child_count": len(children), "completed_at": now,
                "comment": comment, "ready": ready, "moved": moved,
            })
            if target == "To Do" and hasattr(repository, "clear_epic_reopen_requests"):
                repository.clear_epic_reopen_requests(key)
            current_epic["status"] = status
            epic["status"] = status
        retry_epic_transition_followups(ctx)
    except Exception as error:  # noqa: BLE001 - the accepted child transition must not fail on epic follow-up
        log.warning("could not reconcile epic %s: %s", key, error)


def _latest_moves(repository, project: str, fallback: dict[str, datetime] | None) -> dict[str, datetime]:
    """Load current move timestamps, preserving caller-provided data for repository test doubles."""
    latest = dict(fallback or {})
    if hasattr(repository, "latest_project_ticket_moves"):
        for move in repository.latest_project_ticket_moves(project):
            if move.ticket not in latest or move.ts > latest[move.ticket]:
                latest[move.ticket] = move.ts
    return latest


def _refresh_epic_snapshot(repository, project: str, key: str, changed: set[str], first_seen: set[str],
                           preexisting: set[str], latest_moves: dict[str, datetime] | None,
                           epic: dict, children: list[dict], epics: list[dict]
                           ) -> tuple[dict | None, list[dict], list[dict]]:
    """Re-read the complete descendant tree under its lifecycle lock before any Jira write."""
    if not hasattr(repository, "list_project_tickets_for_epic_reconcile"):
        return epic, children, epics
    rows = [asdict(row) if not isinstance(row, dict) else row
            for row in repository.list_project_tickets_for_epic_reconcile((project,))]
    by_key = {row["key"]: row for row in rows}
    current_epic = by_key.get(key)
    if current_epic is None:
        return None, [], []
    return (current_epic, children_for(current_epic, rows, by_key),
            [row for row in rows if row.get("project") == project and
             str(row.get("type") or "").strip().casefold() == "epic"])


def retry_epic_transition_followups(ctx: ApplicationContext) -> None:
    """Retry each persisted event, report, move, audit and comment independently."""
    repository = ticket_repository_of(ctx)
    if not hasattr(repository, "pending_epic_transition_followups"):
        return
    for followup in repository.pending_epic_transition_followups():
        _deliver_followup(ctx, repository, followup, "status_event_done", _status_event)
        if not followup.report_done:
            _deliver_followup(ctx, repository, followup, "report_done", _report_followup)
        if not followup.transition_event_done:
            _deliver_followup(ctx, repository, followup, "transition_event_done", _transition_event)
        if not followup.move_done:
            _deliver_followup(ctx, repository, followup, "move_done", _move_followup)
        if not followup.audit_done:
            _deliver_followup(ctx, repository, followup, "audit_done", _audit_followup)
        if not followup.comment_done:
            _deliver_followup(ctx, repository, followup, "comment_done", _comment_followup)


def _deliver_followup(ctx, repository, followup, field: str, deliver) -> None:
    """Deliver one side effect and commit its completion marker atomically when possible."""
    if getattr(followup, field):
        return
    try:
        deliver(ctx, repository, followup)
    except Exception as error:  # noqa: BLE001 - other pending effects must still be attempted
        log.warning("could not deliver epic %s follow-up %s: %s", followup.ticket, field, error)


def _status_event(ctx, repository, followup) -> None:
    with ctx.db.transaction():
        ctx.events.emit("ticket.status", ticket=followup.ticket, project=followup.project,
                        outcome=followup.to_status, reason=f"from {followup.from_status}",
                        from_status=followup.from_status, ready=followup.ready, source=followup.source)
        repository.finish_epic_transition_followup((followup.id, "status_event_done"))


def _report_followup(ctx, repository, followup) -> None:
    project_lifecycle.report_done_epic(ctx, followup.project, followup.ticket,
                                       lifecycle_key=f"epic-transition:{followup.id}")
    with ctx.db.transaction():
        repository.finish_epic_transition_followup((followup.id, "report_done"))


def _transition_event(ctx, repository, followup) -> None:
    with ctx.db.transaction():
        ctx.events.emit("jira.transition", ticket=followup.ticket, account="control",
                        outcome=followup.to_status, reason=followup.reason)
        repository.finish_epic_transition_followup((followup.id, "transition_event_done"))


def _move_followup(ctx, repository, followup) -> None:
    from . import merge_flow
    merge_flow.record_move(ctx, followup.ticket, followup.from_status, followup.to_status,
                           merge_flow.EPIC_CHILD, followup.reason, at=followup.completed_at,
                           followup_id=followup.id)


def _audit_followup(ctx, repository, followup) -> None:
    repository.finish_epic_followup_audit(followup)


def _comment_followup(ctx, repository, followup) -> None:
    ctx.jira.comment(followup.ticket, followup.comment)
    with ctx.db.transaction():
        repository.finish_epic_transition_followup((followup.id, "comment_done"))


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
                apply_transition(ctx, project, epic, children, epics, target, source,
                                 changed, first_seen, preexisting, latest_moves)
            elif hasattr(repository, "clear_epic_reopen_requests") and epic.get("status") == "Done":
                repository.clear_epic_reopen_requests(epic["key"])


def reconcile_from_watchdog(ctx: ApplicationContext) -> None:
    """Retry failed child-start, completion and recently reopened epic transitions."""
    if not ctx.jira:
        return
    retry_epic_transition_followups(ctx)
    projects = [key for key, project in ctx.repo().projects.items() if not project.paused]
    repository = ticket_repository_of(ctx)
    latest_moves = {
        row.ticket: row.ts
        for project in projects
        for row in repository.latest_project_ticket_moves(project)
    }
    reconcile(ctx, sorted(projects), set(), set(), set(), "watchdog", latest_moves)
