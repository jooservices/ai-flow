"""Candidate collectors for each work step in the work eligibility pipeline."""
from __future__ import annotations

from datetime import timedelta

from ... import environment as environment_reports, hotfiles, tooling, worker_candidates
from .eligibility_scope import Scope


def verify_candidates(scope: Scope) -> list[dict]:
    """Collect verification tickets whose completed development run belongs to another account."""
    if "verify" not in scope.roles:
        return []
    verify_cooling = {row["ticket_key"] for row in scope.backoff_rows("verify")}
    out = []
    for ticket in scope.ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'In Verify'"
                                   " AND NOT ('aiflow-hold' = ANY(labels))" + scope.scoped_key,
                                   (scope.projects, *scope.scoped_ticket)):
        if scope.needs_missing_container_runtime(ticket, "verify"):
            continue
        if ticket["key"] in scope.held or ticket["key"] in verify_cooling:
            continue
        dev = scope.deps.last_dev(ticket["key"], done=True)
        if not dev or not dev["pr_url"]:
            continue
        if dev["account_id"] == scope.account.id:
            continue
        if scope.explicit_only_blocks(ticket):
            continue
        if not scope.allowed("verify", ticket):
            continue
        out.append({**ticket, "step": "verify", "probation": scope.probation("verify")})
    return out


def post_merge_candidates(scope: Scope) -> list[dict]:
    """Collect pending post-merge checks assigned to accounts with the release role."""
    if "release" not in scope.roles:
        return []
    post_merge_cooling = {row["ticket_key"] for row in scope.backoff_rows("post_merge")}
    post_merge_projects = scope.projects
    if scope.ticket_key is not None:
        ticket_project = scope.ctx.db.one("SELECT project FROM tickets WHERE key=%s", (scope.ticket_key,))
        project_key = str((ticket_project or {}).get("project") or "")
        post_merge_projects = [project_key] if project_key in scope.projects else []
    pending = [item for project_key in post_merge_projects
               for item in environment_reports.pending_post_merge(
                   scope.ctx, project_key, [scope.ticket_key] if scope.ticket_key is not None else None)]
    out = []
    for waiting in pending:
        ticket = scope.ctx.db.one("SELECT * FROM tickets WHERE key=%s", (waiting["ticket"],))
        if not ticket or ticket["key"] in scope.held or ticket["key"] in post_merge_cooling:
            continue
        if scope.explicit_only_blocks(ticket):
            continue
        if not scope.allowed("release", ticket):
            continue
        out.append({**ticket, "step": "post_merge", "probation": scope.probation("release")})
    return out


def triage_candidates(scope: Scope) -> list[dict]:
    """Collect open owner escalations for accounts assigned the master role."""
    if "master" not in scope.roles:
        return []
    triage_cooling = {row["ticket_key"] for row in scope.backoff_rows("triage")}
    out = []
    # Triage decides a raised escalation; a ticket in Needs owner without an open one has nothing to decide.
    for ticket in scope.ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Needs owner'"
                                   " AND NOT ('owner-escalated' = ANY(labels)) AND NOT ('aiflow-hold' = ANY(labels))"
                                   " AND EXISTS (SELECT 1 FROM escalations e WHERE e.ticket_key = tickets.key"
                                   " AND e.resolution = 'open')" + scope.scoped_tickets_key,
                                   (scope.projects, *scope.scoped_ticket)):
        if scope.needs_missing_container_runtime(ticket, "triage"):
            continue
        if ticket["key"] in scope.held or ticket["key"] in triage_cooling:
            continue
        dev = scope.deps.last_dev(ticket["key"])
        if dev and dev["account_id"] == scope.account.id:
            continue
        if scope.explicit_only_blocks(ticket):
            continue
        if not scope.allowed("master", ticket):
            continue
        out.append({**ticket, "step": "triage", "probation": scope.probation("master")})
    return out


def _recent_intake_backoff(scope: Scope, step: str) -> set[str]:
    return {row["ticket_key"] for row in scope.intake_backoff_rows(step)
            if row["outcome"] in ("failed", "timeout", "lost") and row["ended_at"]
            and row["ended_at"] > scope.now() - timedelta(minutes=10)}


def intake_candidates(scope: Scope) -> list[dict]:
    """Collect new Intake tickets for the account's intake-refiner role."""
    if "intake_refiner" not in scope.roles:
        return []
    waiting = _recent_intake_backoff(scope, "intake")
    out = []
    for ticket in scope.ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Intake'"
                                   " AND NOT ('aiflow-hold' = ANY(labels))" + scope.scoped_key
                                   + " ORDER BY jira_created NULLS LAST, key",
                                   (scope.projects, *scope.scoped_ticket)):
        if scope.needs_missing_container_runtime(ticket, "intake"):
            continue
        if ticket["key"] in scope.held or ticket["key"] in waiting:
            continue
        if scope.explicit_only_blocks(ticket):
            continue
        if not scope.allowed("intake_refiner", ticket):
            continue
        out.append({**ticket, "step": "intake", "probation": scope.probation("intake_refiner")})
    return out


def impact_candidates(scope: Scope) -> list[dict]:
    """Collect unassessed change requests waiting in Backlog for impact assessment."""
    if "intake_refiner" not in scope.roles:
        return []
    waiting = _recent_intake_backoff(scope, "impact")
    out = []
    for ticket in scope.ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Backlog'"
                                   " AND 'change-request' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))"
                                   " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = tickets.key"
                                   " AND r.step = 'impact' AND r.outcome = 'assessed')" + scope.scoped_tickets_key
                                   + " ORDER BY jira_created NULLS LAST, key",
                                   (scope.projects, *scope.scoped_ticket)):
        if scope.needs_missing_container_runtime(ticket, "impact"):
            continue
        if ticket["key"] in scope.held or ticket["key"] in waiting:
            continue
        if scope.explicit_only_blocks(ticket):
            continue
        if not scope.allowed("intake_refiner", ticket):
            continue
        out.append({**ticket, "step": "impact", "probation": scope.probation("intake_refiner")})
    return out


def _dev_ticket_data(scope: Scope) -> tuple[list[dict], set[str], set[str], set[str]]:
    dev_tickets = scope.ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s)"
                                   + worker_candidates.DEV_TICKET_SQL + scope.scoped_key,
                                   (scope.projects, *scope.scoped_ticket))
    blocker_keys = sorted({str(blocker) for ticket in dev_tickets
                           for blocker in ticket.get("blocked_by") or []})
    done = ({row["key"] for row in scope.ctx.db.all(
        "SELECT key FROM tickets WHERE key=ANY(%s) AND status='Done'", (blocker_keys,))}
            if blocker_keys else set())
    dev_keys = [ticket["key"] for ticket in dev_tickets]
    history_keys = ({row["ticket_key"] for row in scope.ctx.db.all(
        "SELECT DISTINCT ticket_key FROM runs WHERE ticket_key = ANY(%s) AND step='dev' AND outcome='done'"
        " AND details ? 'changed_files'", (dev_keys,))} if dev_keys else set())
    blocked_keys = ({row["ticket_key"] for row in scope.ctx.db.all(
        "SELECT ticket_key FROM hot_file_blocked WHERE ticket_key = ANY(%s)", (dev_keys,))}
                    if dev_keys else set())
    return dev_tickets, done, history_keys, blocked_keys


class HotFileGate:
    """Cache development footprints and keep hot-file reservations from overlapping."""

    def __init__(self, scope: Scope, history_keys: set[str], blocked_keys: set[str]):
        self.scope = scope
        self.history_keys = history_keys
        self.blocked_keys = blocked_keys
        self.epics: dict[str, str] = {}
        self.footprints: dict[str, dict] = {}
        self.footprint_history: dict[str, bool] = {}
        self.flying: dict[str, list[str]] | None = None

    def footprint(self, row: dict, include_history: bool = False) -> dict:
        key = str(row["key"])
        if key not in self.epics:
            self.epics[key] = self.scope.deps.ticket_epic(row)
        include = include_history or bool(self.epics[key]) or key in self.history_keys
        if key not in self.footprints or (include and not self.footprint_history.get(key, False)):
            self.footprints[key] = hotfiles.predict(self.scope.ctx.db, row, self.epics[key],
                                                    include_history=include)
            self.footprint_history[key] = include
        return self.footprints[key]

    def admits(self, ticket: dict) -> bool:
        candidate_footprint = self.footprint(ticket)
        if not candidate_footprint["hot"]:
            if ticket["key"] in self.blocked_keys:
                hotfiles.clear_blocked(self.scope.ctx.db, ticket["key"])
                self.blocked_keys.discard(ticket["key"])
            return True
        if self.flying is None:
            self.flying = hotfiles.in_flight_hot_files(
                self.scope.ctx.db, lambda row: self.footprint(row, include_history=True))
        # A hot-file holder keeps its reservation through development and review; waiting never permits overlap.
        if hotfiles.blocking_ticket(candidate_footprint,
                                    {key: value for key, value in self.flying.items() if key != ticket["key"]},
                                    slots=self.scope.ctx.settings.hot_file_slots):
            if ticket["key"] not in self.blocked_keys:
                hotfiles.mark_blocked(self.scope.ctx.db, ticket["key"])
                self.blocked_keys.add(ticket["key"])
            return False
        if ticket["key"] in self.blocked_keys:
            hotfiles.clear_blocked(self.scope.ctx.db, ticket["key"])
            self.blocked_keys.discard(ticket["key"])
        return True


def _may_offer_dev(scope: Scope, ticket: dict, done: set[str], cooling: set[str], parked: set[str]) -> bool:
    if scope.needs_missing_container_runtime(ticket, "dev"):
        return False
    if not worker_candidates.may_offer_dev(ticket, done, held=scope.held, cooling=cooling, parked=parked):
        return False
    labels = set(ticket["labels"])
    if "tier-senior" in labels and scope.account.tier != "senior":
        return False
    if scope.account.explicit_only and f"ai-{scope.account.provider}" not in labels:
        return False
    return scope.allowed("dev", ticket)


def dev_candidates(scope: Scope) -> list[dict]:
    """Collect eligible development tickets and maintain their hot-file block reservations."""
    if "dev" not in scope.roles:
        return []
    cooling = {row["ticket_key"] for row in scope.backoff_rows("dev")}
    candidate_scope = [scope.ticket_key] if scope.ticket_key is not None else None
    parked = tooling.parked_tickets(scope.ctx, scope.node_row, candidate_scope)
    parked |= environment_reports.parked_tickets(scope.ctx, candidate_scope)
    dev_tickets, done, history_keys, blocked_keys = _dev_ticket_data(scope)
    gate = HotFileGate(scope, history_keys, blocked_keys)
    dev_rows = []
    for ticket in dev_tickets:
        if not _may_offer_dev(scope, ticket, done, cooling, parked):
            continue
        if not gate.admits(ticket):
            continue
        dev_rows.append({**ticket, "step": "dev", "probation": scope.probation("dev")})
    dev_rows.sort(key=lambda ticket: (scope.project_priority.get(ticket["project"], 5),
                                     "rework" not in ticket["labels"],
                                     scope.ticket_priority.get(ticket["priority"], 2),
                                     ticket["jira_created"] or scope.now()))
    return dev_rows
