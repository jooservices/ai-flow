"""Table-driven parent epic lifecycle and shared transition/audit handling."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from .repositories.tickets import TicketsRepository
from . import projects as project_lifecycle

log = logging.getLogger("aiflow4.epic_lifecycle")

EPIC_FINISHED = ("Done", "Declined")
EPIC_NOT_STARTED = ("Backlog", "Intake", "To Do")


@dataclass(frozen=True)
class Transition:
    target: str
    sources: tuple[str, ...] | None
    guard: str
    reason: str


def _all_children_finished(_epic: dict, children: list[dict], *_args) -> bool:
    return bool(children) and all(child["status"] in EPIC_FINISHED for child in children)


def _first_child_started(_epic: dict, children: list[dict], *_args) -> bool:
    return any(child["status"] not in EPIC_NOT_STARTED and child["status"] not in EPIC_FINISHED
               for child in children)


def _new_active_child(epic: dict, children: list[dict], changed: set[str], first_seen: set[str],
                      preexisting: set[str]) -> bool:
    return any(child["key"] in changed and child["status"] not in EPIC_FINISHED and
               (child["key"] not in first_seen or epic["key"] in preexisting) for child in children)


GUARDS = {"all_children_finished": _all_children_finished,
          "first_child_started": _first_child_started,
          "new_active_child": _new_active_child}

TRANSITIONS = (
    Transition("Done", None, "all_children_finished",
               "all {count} child tickets are Done or Declined"),
    Transition("In Progress", EPIC_NOT_STARTED, "first_child_started", "the first child ticket started"),
    Transition("To Do", ("Done",), "new_active_child", "{count} child tickets are not all Done"),
)


def is_under_epic(ticket: dict, epic_key: str, by_key: dict[str, dict]) -> bool:
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
    return [row for row in rows if row["type"] != "Epic" and is_under_epic(row, epic["key"], by_key)]


def next_transition(epic: dict, children: list[dict], changed: set[str], first_seen: set[str],
                    preexisting: set[str]) -> Transition | None:
    for transition in TRANSITIONS:
        if transition.sources is not None and epic["status"] not in transition.sources:
            continue
        guard = GUARDS[transition.guard]
        if guard(epic, children, changed, first_seen, preexisting) and epic["status"] != transition.target:
            return transition
    return None


def _reason(transition: Transition, child_count: int) -> str:
    return transition.reason.format(count=child_count)


def apply_transition(ctx, project: str, epic: dict, children: list[dict], epics: list[dict],
                     transition: Transition) -> None:
    """Apply one selected transition to Jira and its mirror, recording the same audit for every guard."""
    key, target = epic["key"], transition.target
    previous = epic["status"]
    detail = _reason(transition, len(children))
    try:
        current = ctx.jira.status(key)
        moved = current != target
        status = ctx.jira.transition_to(key, target) if moved else current
        if status != target:
            return
        now = datetime.now(timezone.utc)
        TicketsRepository(ctx.db).record_epic_transition(key, status, now, transition.guard, detail,
                                                          len(children), moved)
        epic["status"] = status
        ready = "aiflow-ready" in (epic.get("labels") or [])
        ctx.events.emit("ticket.status", ticket=key, project=project, outcome=status,
                        reason=f"from {previous}", from_status=previous, ready=ready, source="jira_sync")
        if target == "Done":
            _report_epic(ctx, project, key)
        if not moved:
            return
        ctx.events.emit("jira.transition", ticket=key, account="control", outcome=status, reason=detail)
        _comment_completion(ctx, project, key, len(children), now, epics, epic, target)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not reconcile epic %s: %s", key, exc)


def _report_epic(ctx, project: str, key: str) -> None:
    try:
        project_lifecycle.report_done_epic(ctx, project, key)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not generate completion report for epic %s: %s", key, exc)


def _comment_completion(ctx, project: str, key: str, child_count: int, now: datetime,
                        epics: list[dict], epic: dict, target: str) -> None:
    if target != "Done":
        return
    report = (ctx.settings.public_url.rstrip("/") + f"/v1/projects/{quote(project, safe='')}/report"
              f"?epic={quote(key, safe='')}&format=html")
    comment = ("AI Flow · epic completed\n" f"Child tickets: {child_count} (all Done)\n"
               f"Completed at: {now.isoformat(timespec='seconds')}\nReport: {report}")
    project_done = bool(epics) and all(row["status"] == "Done" or row["key"] == key for row in epics)
    if project_done:
        project_report = (ctx.settings.public_url.rstrip("/") +
                          f"/v1/projects/{quote(project, safe='')}/report?format=html")
        comment += (f"\nProject suggestion: all {len(epics)} epics are Done; "
                    f"consider closing project {project}. Report: {project_report}")
    ctx.jira.comment(key, comment)


def reconcile(ctx, projects: list[str], changed: set[str], first_seen: set[str], preexisting: set[str]) -> None:
    repository = TicketsRepository(ctx.db)
    for project in projects:
        rows = repository.project_epic_rows(project)
        by_key = {row["key"]: row for row in rows}
        epics = [row for row in rows if row["type"] == "Epic"]
        for epic in epics:
            children = children_for(epic, rows, by_key)
            transition = next_transition(epic, children, changed, first_seen, preexisting)
            if transition:
                apply_transition(ctx, project, epic, children, epics, transition)
