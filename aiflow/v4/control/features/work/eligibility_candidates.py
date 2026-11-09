"""Candidate collectors for each work step in the work eligibility pipeline."""
from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException

from ... import environment as environment_reports, hotfiles, load_policy, role_health, rollout, tooling, worker_candidates
from ...repositories.runs import VerifyTicketHistory, verify_retry_backoff, verify_ticket_history
from .eligibility_scope import Scope
from .repository import WorkRepository

VERIFY_NO_VERDICT_RETRY_WAIT = timedelta(minutes=15)


def verify_candidates(scope: Scope) -> list[dict]:
    """Collect verification tickets whose completed development run belongs to another account."""
    if "verify" not in scope.roles:
        return []
    verify_cooling = verify_retry_backoff(scope.ctx.db, scope.ticket_key)
    tickets = scope.ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'In Verify'"
                               " AND NOT ('aiflow-hold' = ANY(labels))" + scope.scoped_key,
                               (scope.projects, *scope.scoped_ticket))
    history = verify_ticket_history(scope.ctx.db, [ticket["key"] for ticket in tickets])
    out = []
    peer_eligibility_cache: dict[tuple[str, str], set[str]] = {}
    for ticket in tickets:
        ticket_history = history.get(ticket["key"], VerifyTicketHistory())
        candidate = _verify_candidate(scope, ticket, verify_cooling, ticket_history, peer_eligibility_cache)
        if not candidate:
            continue
        if scope.verify_retry_guard and not _verify_retry_fallback_due(scope, ticket_history):
            # Keep the first reviewer when eligible; peer probes skip this preference to avoid mutual eligibility.
            first = first_review_account(scope, ticket["key"])
            if first and first != scope.account.id and preferred_review_available(scope, first, ticket):
                continue
        out.append(candidate)
    return out


def _verify_dev(scope: Scope, ticket: dict, verify_cooling: set[str]) -> dict | None:
    """The latest development run of a ticket this account may verify: a green local gate, runtime, lease, author,
    provider-label and role checks; None when it may not."""
    if scope.needs_missing_container_runtime(ticket, "verify"):
        return None
    if ticket["key"] in scope.held or ticket["key"] in verify_cooling:
        return None
    dev = WorkRepository(scope.ctx.db).latest_dev_run(ticket["key"])
    if not _green_dev_submission(dev) or dev["account_id"] == scope.account.id:
        return None
    if scope.explicit_only_blocks(ticket) or not scope.allowed("verify", ticket):
        return None
    return dev


def _verify_candidate_ready(scope: Scope, ticket: dict, verify_cooling: set[str]) -> bool:
    """Apply the existing runtime, lease, author, provider-label and role checks for a verify candidate."""
    return _verify_dev(scope, ticket, verify_cooling) is not None


def _verify_candidate(scope: Scope, ticket: dict, cooling: set[str],
                      history: VerifyTicketHistory,
                      peer_eligibility_cache: dict[tuple[str, str], set[str]] | None = None) -> dict | None:
    dev = _verify_dev(scope, ticket, cooling)
    if dev is None:
        return None
    if scope.verify_retry_guard and _verify_retry_blocked(
            scope, ticket, dev["account_id"], history, peer_eligibility_cache):
        return None
    return {**ticket, "step": "verify", "probation": scope.probation("verify")}


def _verify_account_barred(attempts: list[bool]) -> bool:
    """Two consecutive no-verdict results (newest first) bar the account from this ticket's current round."""
    return len(attempts) >= 2 and bool(attempts[0] and attempts[1])


def _verify_retry_fallback_due(scope: Scope, history: VerifyTicketHistory) -> bool:
    """Allow a prior no-verdict verifier back after peers had time to claim the retry."""
    attempts = history.by_account.get(scope.account.id, [])
    ended_at = history.last_no_verdict_ended_at
    return bool(any(attempts) and ended_at and scope.now() - ended_at >= VERIFY_NO_VERDICT_RETRY_WAIT)


def _verify_retry_blocked(scope: Scope, ticket: dict, dev_account: str, history: VerifyTicketHistory,
                          peer_eligibility_cache: dict[tuple[str, str], set[str]] | None = None) -> bool:
    attempts = history.by_account.get(scope.account.id, [])
    if _verify_account_barred(attempts):
        return True
    if not any(attempts):
        return False
    if _verify_retry_fallback_due(scope, history):
        return False
    return _untried_verify_peer(scope, ticket, dev_account, history, peer_eligibility_cache)


def _untried_verify_peer(scope: Scope, ticket: dict, dev_account: str,
                         history: VerifyTicketHistory,
                         peer_eligibility_cache: dict[tuple[str, str], set[str]] | None = None) -> bool:
    peer_eligibility_cache = peer_eligibility_cache if peer_eligibility_cache is not None else {}
    for account in scope.ctx.repo().accounts:
        if not _verify_retry_peer_account(account, scope.account.id, dev_account, history):
            continue
        for node in dict.fromkeys(account.machines):
            if ticket["key"] in _peer_verify_tickets(scope, account.id, node, peer_eligibility_cache):
                return True
    return False


def _peer_verify_tickets(scope: Scope, account_id: str, node: str,
                         cache: dict[tuple[str, str], set[str]]) -> set[str]:
    """Load a peer machine's verify offers once per poll, then reuse them for each ticket probe."""
    key = account_id, node
    if key in cache:
        return cache[key]
    if not load_policy.claim_limits(scope.ctx, node)["allowed"]:
        cache[key] = set()
        return cache[key]
    if not rollout.node_compatible(scope.ctx, node, scope.deps.releases())["compatible"]:
        cache[key] = set()
        return cache[key]
    options = {"routing": False, "capacity_routing": False, "step_filter": "verify",
               "verify_retry_guard": False}
    if scope.ticket_key is not None:
        options["ticket_key"] = scope.ticket_key
    try:
        offers = scope.deps.eligible(account_id, node, **options)
    except HTTPException:
        offers = []
    cache[key] = {item["key"] for item in offers if item.get("step") == "verify"}
    return cache[key]


def _verify_retry_peer_account(account, account_id: str, dev_account: str,
                               history: VerifyTicketHistory) -> bool:
    return (account.id not in {account_id, dev_account} and not account.paused
            and "verify" in role_health.account_roles(account)
            and not any(history.by_account.get(account.id, [])))


def _green_dev_submission(dev: dict | None) -> bool:
    """Accept only a finished successful dev run with a recorded green local gate and pull request."""
    details = (dev or {}).get("details") or {}
    local_gate = details.get("local_gate") if isinstance(details, dict) else None
    return bool(dev and dev.get("status") == "finished" and dev.get("outcome") == "done"
                and dev.get("pr_url") and isinstance(local_gate, dict) and local_gate.get("status") == "green")


def first_review_account(scope: Scope, ticket_key: str) -> str:
    """Find the account from the earliest completed AI review of this ticket."""
    return WorkRepository(scope.ctx.db).first_review_account(ticket_key)


def preferred_review_available(scope: Scope, account_id: str, ticket: dict) -> bool:
    """Check the preferred review account through the normal eligibility rules on its machines."""
    account = scope.repo.account(account_id)
    if not account:
        return False
    for node in account.machines:
        candidates = scope.deps.eligible(account_id, node, routing=False, capacity_routing=False,
                                         ticket_key=ticket["key"], step_filter="verify")
        if any(item.get("key") == ticket["key"] and item.get("step") == "verify" for item in candidates):
            return True
    return False


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
                                   " AND NOT ('aiflow-hold' = ANY(labels))"
                                   " AND NOT ('aiflow-split-parent' = ANY(labels))" + scope.scoped_key
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
