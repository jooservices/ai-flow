"""Role health: quality by AI account and role (docs/v4/role-health.md).

Completed pull requests and clean no-change results are successes; failures, timeouts and real tool-missing stops
are scored (a timeout or a lost lease at a quarter of a failure, since the machine and the size of the ticket are often the cause), while quota, sign-in and machine problems are excluded. A role can be switched off on only a subset of
its ready accounts: never the last two, and never more than half. When every such account scores
badly, all remain on and one role-level incident reports the common cause. Disabled roles return as soon as their
history no longer exceeds the threshold or the recorded missing tool is available; otherwise successful probation
runs can restore them without a calendar-day delay.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .incidents import close_incident, open_incident
from .repositories.runs import RunsRepository
from ..core.toolchains import parse_missing_requirements

log = logging.getLogger("aiflow4.role_health")

# The API and watchdog can evaluate role health at the same time. This lock avoids same-process
# evaluations holding pool connections while waiting for PostgreSQL; the advisory lock also covers
# evaluations from another control process.
_EVALUATION_LOCK = threading.Lock()
_EVALUATION_ADVISORY_LOCK = (261, 261)

DEFAULT_WINDOW = 10
DEFAULT_FAILURE_PERCENT = 60
WINDOW_LIMITS = (1, 100)
PERCENT_LIMITS = (0, 99)
RECOVERY_STREAK = 2          # probation tickets in a row that must succeed
METRICS_DAYS = 30            # cost and rework are shown for the last 30 days
HISTORY_RUNS = 500           # finished runs read per account
DEFAULT_ROLES = ("dev", "verify")
STEP_ROLE = {"dev": "dev", "verify": "verify", "triage": "master", "master": "master",
             "intake": "intake_refiner", "impact": "intake_refiner", "post_merge": "release"}
# error kinds that mean the account or its machine could not work, not that the AI did bad work
UNSCORED_KINDS = {"quota", "auth", "infra", "network", "machine", "node", "tool", "tools", "missing_tool",
                  "missing-tool", "offline", "toolchain", "services"}
SUCCESS_OUTCOMES = {"merged", "human", "refined", "questions", "assessed", "answered", "escalated"}
STOPPED_OUTCOMES = {"timeout", "lost"}
STOPPED_KINDS = {"timeout", "lost"}  # a timed-out run is stored as outcome failed with this error kind
STOPPED_WEIGHT = 0.25        # share of a failure that a run stopped by the time limit or a lost lease counts

@dataclass(frozen=True)
class Scored:
    label: str                  # finished | merged | sent_back | failed | no_changes | stopped | excluded
    failure: float | None       # 0, 0.5 or 1; None when the run is not scored
    rework: bool = False        # a rework round: the run was sent back, or a verify run that sent work back


def role_of(step: str) -> str | None:
    return STEP_ROLE.get(step)


def classify(run: dict) -> Scored | None:
    """Score one finished run; None while verification has not yet judged a finished dev run."""
    outcome, kind = run["outcome"], (run.get("error_kind") or "").lower()
    summary = (run.get("summary") or "").strip().lower()
    details = run.get("details") or {}
    if outcome == "merged":
        return Scored("merged", 0.0)
    if details.get("no_verdict") is True and run.get("step") == "verify":
        # Counts against the verify role only as the second no-verdict result in a row of this account on this
        # ticket (docs/v4/verify-no-verdict.md); the first is retried on another account and not scored.
        return Scored("failed", 1.0) if run.get("prev_no_verdict") else Scored("excluded", None)
    no_work_failure = outcome == "failed" and kind == "no_work" and details.get("no_changes") is True
    legacy_no_changes = (run.get("step") == "dev" and outcome == "failed" and not kind
                         and summary.startswith(("no changes", "nothing to change")))
    if outcome == "no_changes" or no_work_failure or legacy_no_changes:
        return Scored("no_changes", 0.0)
    if outcome == "quota" or kind in UNSCORED_KINDS:
        return Scored("excluded", None)
    if outcome == "tool_missing":
        if not _valid_tool_missing_evidence(run):
            # Old classifier rows often only stored the AI's prose. With no explicit marker or normalized command
            # result, they cannot safely remain failures in today's score.
            return Scored("excluded", None)
        return Scored("tool_missing", 1.0)
    if outcome == "done" and run["step"] == "dev":
        details = run.get("details") or {}
        if run.get("pr_url") and details.get("pr_url_source") == "current":
            return Scored("finished", 0.0)
        decision = run.get("decision")
        if decision in ("merged", "human"):
            return Scored("finished", 0.0)
        if decision == "rework":
            return Scored("sent_back", 0.5, rework=True)
        return None
    if outcome == "rework":  # the verifier delivered its verdict; the work it judged is the developer's
        return Scored("finished", 0.0, rework=True)
    if outcome == "done" or outcome in SUCCESS_OUTCOMES:
        return Scored("finished", 0.0)
    if outcome in STOPPED_OUTCOMES or (outcome == "failed" and kind in STOPPED_KINDS):
        # Stopped by the time limit or the loss of its lease: mostly the size of the ticket or the load of the
        # machine, not the quality of the AI. It weighs a quarter of a failed run, so at the default 60% threshold a
        # busy machine alone cannot switch a role off (it did: 7 of the last 10 runs of the best account were
        # timeouts on an overloaded host).
        return Scored("stopped", STOPPED_WEIGHT)
    if outcome == "blocked":
        return Scored("blocked", 1.0)
    if outcome == "failed":
        return Scored("failed", 1.0)
    return Scored("excluded", None)


def _valid_tool_missing_evidence(run: dict) -> bool:
    """Accept current and legacy tool-missing rows only when their stored evidence is verifiable."""
    details = run.get("details") or {}
    missing = {str(name).casefold() for name in (details.get("missing_tools") or []) if str(name).strip()}
    if not missing:
        return False
    marker_names = {name.casefold() for name in parse_missing_requirements(str(details.get("verdict_reason") or ""))}
    if missing & marker_names:
        return True
    if details.get("tool_missing_evidence") != "command_not_found":
        return False
    executed_names = {str(name).casefold() for name in (details.get("command_not_found_tools") or [])
                      if str(name).strip()}
    return bool(missing & executed_names)


def get_settings(db) -> dict[str, Any]:
    row = db.one("SELECT window_runs, failure_percent, updated_by, updated_at FROM role_health_settings")
    if not row:
        return {"window_runs": DEFAULT_WINDOW, "failure_percent": DEFAULT_FAILURE_PERCENT, "updated_by": "",
                "updated_at": None, "defaults": {"window_runs": DEFAULT_WINDOW, "failure_percent": DEFAULT_FAILURE_PERCENT}}
    return {"window_runs": int(row["window_runs"]), "failure_percent": int(row["failure_percent"]),
            "updated_by": row["updated_by"], "updated_at": row["updated_at"].isoformat(),
            "defaults": {"window_runs": DEFAULT_WINDOW, "failure_percent": DEFAULT_FAILURE_PERCENT}}


def set_settings(db, window_runs: int, failure_percent: int, by: str) -> dict[str, Any]:
    if not WINDOW_LIMITS[0] <= window_runs <= WINDOW_LIMITS[1]:
        raise ValueError(f"window must be between {WINDOW_LIMITS[0]} and {WINDOW_LIMITS[1]} runs")
    if not PERCENT_LIMITS[0] <= failure_percent <= PERCENT_LIMITS[1]:
        raise ValueError(f"threshold must be between {PERCENT_LIMITS[0]} and {PERCENT_LIMITS[1]} percent")
    db.run("INSERT INTO role_health_settings (id, window_runs, failure_percent, updated_by) VALUES (true,%s,%s,%s)"
           " ON CONFLICT (id) DO UPDATE SET window_runs=EXCLUDED.window_runs, failure_percent=EXCLUDED.failure_percent,"
           " updated_by=EXCLUDED.updated_by, updated_at=now()", (window_runs, failure_percent, by[:200]))
    return get_settings(db)


def _history(db, account_id: str = "") -> dict[tuple[str, str], list[tuple[dict, Scored | None]]]:
    """Newest first, per (account, role): every finished run with its score (None = not decided yet)."""
    out: dict[tuple[str, str], list[tuple[dict, Scored | None]]] = {}
    for run in RunsRepository(db).role_health_history(list(STEP_ROLE), account_id, HISTORY_RUNS):
        out.setdefault((run["account_id"], STEP_ROLE[run["step"]]), []).append((run, classify(run)))
    return out


def failure_score(history: list[tuple[dict, Scored | None]], window: int,
                  scoring_from: datetime | None = None) -> tuple[float | None, int]:
    """(score, eligible runs used): the failure score over the latest `window` scored runs, None below a full window."""
    eligible = [s for r, s in history if s is not None and s.failure is not None
                and (scoring_from is None or r["started_at"] >= scoring_from)][:window]
    if len(eligible) < window:
        return None, len(eligible)
    return sum(s.failure for s in eligible) / len(eligible), len(eligible)


def account_roles(account) -> list[str]:
    return list(account.roles or DEFAULT_ROLES)


def _states(db) -> dict[tuple[str, str], dict]:
    return {(r["account_id"], r["role"]): r for r in db.all("SELECT * FROM role_health")}


def disabled_roles(db) -> set[tuple[str, str]]:
    return {(r["account_id"], r["role"]) for r in db.all("SELECT account_id, role FROM role_health WHERE NOT enabled")}


# --- probation -------------------------------------------------------------------------------------------------
_UNIT_TEST = re.compile(r"unit[ -]?tests?", re.IGNORECASE)
_UI = re.compile(r"\b(ui|ux|user interface|front-?end|screen|page|browser|button|css|html|console|dashboard|layout)\b",
                 re.IGNORECASE)
SMALL_DESCRIPTION_CHARS = 2000


def probation_ticket(ticket: dict) -> bool:
    """An easy ticket: a small Bug or Chore whose acceptance criteria ask for a unit test, with no UI."""
    text = ticket.get("description") or ""
    labels = {str(x).lower() for x in (ticket.get("labels") or [])}
    return (str(ticket.get("type") or "").lower() in ("bug", "chore") and len(text) <= SMALL_DESCRIPTION_CHARS
            and bool(_UNIT_TEST.search(text)) and not _UI.search(f"{ticket.get('summary') or ''} {text}")
            and not labels & {"ui", "ux", "frontend", "tier-senior"})


def modes(db, account_id: str, roles: list[str]) -> dict[str, str]:
    """Per role of one account: on, probation (switched off, no ticket is running) or off."""
    result = {role: "on" for role in roles}
    for row in db.all("SELECT role FROM role_health WHERE account_id=%s AND NOT enabled", (account_id,)):
        if row["role"] in result:
            result[row["role"]] = "probation" if probation_slot_open(db, account_id, row["role"]) else "off"
    return result


def probation_slot_open(db, account_id: str, role: str) -> bool:
    return not db.one("SELECT 1 AS x FROM role_probation WHERE account_id=%s AND role=%s AND result=''",
                      (account_id, role))


def assign_probation(db, account_id: str, role: str, ticket_key: str) -> int | None:
    """Take the available probation slot; one unresolved ticket per account and role."""
    row = db.one("INSERT INTO role_probation (account_id, role, ticket_key) SELECT %s,%s,%s WHERE NOT EXISTS ("
                 " SELECT 1 FROM role_probation WHERE account_id=%s AND role=%s AND result='')"
                 " RETURNING id", (account_id, role, ticket_key, account_id, role))
    return row["id"] if row else None


def attach_probation_run(db, probation_id: int, run_id: str) -> None:
    db.run("UPDATE role_probation SET run_id=%s WHERE id=%s", (run_id, probation_id))


def drop_probation(db, probation_id: int) -> None:
    db.run("DELETE FROM role_probation WHERE id=%s AND result=''", (probation_id,))


def _resolve_probation(ctx, account_id: str = "") -> None:
    """Record the result of open probation tickets once their run is scored; a run that ended on quota or a machine
    problem (or never ran) gives the slot back."""
    for p in ctx.db.all("SELECT * FROM role_probation WHERE result='' AND (%s='' OR account_id=%s)", (account_id, account_id)):
        run = ctx.db.one("SELECT id, ticket_key, step, account_id, outcome, error_kind, summary, details, pr_url, status, started_at,"
                         " CASE WHEN step='dev' AND outcome='done' THEN (SELECT v.outcome FROM runs v WHERE"
                         " v.ticket_key=runs.ticket_key AND v.step IN ('verify','qa') AND v.started_at > runs.started_at"
                         " AND v.outcome IN ('merged','human','rework') ORDER BY v.started_at LIMIT 1) END AS decision"
                         " FROM runs WHERE id=%s", (p["run_id"],)) if p["run_id"] else None
        if not run:
            if p["assigned_at"] < datetime.now(timezone.utc) - timedelta(hours=1):
                drop_probation(ctx.db, p["id"])
            continue
        if run["status"] == "running":
            continue
        if not run["outcome"]:  # released without a result
            drop_probation(ctx.db, p["id"])
            continue
        score = classify(run)
        if score is None:
            continue
        if score.failure is None:
            drop_probation(ctx.db, p["id"])
            continue
        result = "success" if score.failure == 0 else "failure"
        ctx.db.run("UPDATE role_probation SET result=%s, resolved_at=now() WHERE id=%s", (result, p["id"]))
        ctx.events.emit("role.probation", ticket=p["ticket_key"], account=p["account_id"], step=p["role"],
                        outcome=result, reason=f"probation ticket {p['ticket_key']} for {p['role']}: {result}")


def _streak(db, account_id: str, role: str, since: datetime | None) -> int:
    rows = db.all("SELECT result FROM role_probation WHERE account_id=%s AND role=%s AND result<>''"
                  " AND (%s::timestamptz IS NULL OR assigned_at >= %s::timestamptz) ORDER BY assigned_at DESC LIMIT %s",
                  (account_id, role, since, since, RECOVERY_STREAK))
    streak = 0
    for row in rows:
        if row["result"] != "success":
            break
        streak += 1
    return streak


# --- switching ------------------------------------------------------------------------------------------------
def _percent(value: float) -> str:
    return f"{round(value * 100):d}%"


FAILURE_KINDS = ("failed", "timeout", "tool_missing", "rework")


def _failure_kind(run: dict, score: Scored) -> str | None:
    if score.failure is None or score.failure <= 0:
        return None
    if score.rework or score.label == "sent_back":
        return "rework"
    if run.get("outcome") == "tool_missing":
        return "tool_missing"
    if run.get("outcome") in STOPPED_OUTCOMES or (run.get("error_kind") or "").lower() == "timeout":
        return "timeout"
    return "failed"


def _scored_window(history, window: int, scoring_from: datetime | None = None) -> list[tuple[dict, Scored]]:
    return [(run, score) for run, score in history if score is not None and score.failure is not None
            and (scoring_from is None or run["started_at"] >= scoring_from)][:window]


def _failure_breakdown(items: list[tuple[dict, Scored]]) -> tuple[dict[str, int], list[str]]:
    counts = {kind: 0 for kind in FAILURE_KINDS}
    tickets = set()
    for run, score in items:
        kind = _failure_kind(run, score)
        if kind:
            counts[kind] += 1
            if run.get("ticket_key"):
                tickets.add(run["ticket_key"])
    return counts, sorted(tickets)


def _diagnostic_detail(items: list[tuple[dict, Scored]], window: int, score: float | None = None) -> str:
    counts, tickets = _failure_breakdown(items)
    scored = len(items)
    prefix = f"Failure score {_percent(score)} over the latest {scored} scored runs (window {window}).\n" if score is not None else ""
    return (prefix + "Failure kinds: " + ", ".join(f"{kind}={counts[kind]}" for kind in FAILURE_KINDS)
            + ".\nTickets involved: " + (", ".join(tickets) if tickets else "none"))


def _record_role_incident(ctx, subject: str, title: str, detail: str) -> None:
    if not open_incident(ctx, "role_health", subject, "warning", title, detail):
        ctx.db.run("UPDATE incidents SET title=%s, detail=%s WHERE kind='role_health' AND subject=%s AND closed_at IS NULL",
                   (title, detail, subject))


def _role_capacity(account_count: int) -> int:
    """Maximum disabled roles while keeping two enabled and disabling no more than half the pool."""
    return max(0, min(account_count // 2, account_count - 2))


def _ready_role_accounts(ctx, accounts: list) -> list:
    """Accounts that can take work now: unpaused, usable account status and at least one online assigned machine."""
    if not accounts:
        return []
    by_id = {account.id: account for account in accounts}
    statuses = {row["id"]: row["status"] for row in ctx.db.all(
        "SELECT id,status FROM accounts WHERE id = ANY(%s)", (list(by_id),))}
    online = {row["name"] for row in ctx.db.all("SELECT name FROM nodes WHERE status='online'")}
    return [account for account in accounts if not account.paused
            and statuses.get(account.id) in ("idle", "working", "probing")
            and online.intersection(account.machines)]


def _tool_missing_cause_fixed(ctx, account, runs, window: int) -> bool:
    """True when every tool-missing result in the current scoring window is available to this account now."""
    missing_runs = [(run, score) for run, score in _scored_window(runs, window)
                    if run.get("outcome") == "tool_missing" and score.failure and score.failure > 0]
    if not missing_runs:
        return False
    from . import tooling
    node_rows = {row["name"]: row for row in ctx.db.all(
        "SELECT name, status, tools, metrics FROM nodes WHERE status='online' AND name = ANY(%s)",
        (account.machines,))}
    if not node_rows:
        return False
    for run, _ in missing_runs:
        requirements = list(dict.fromkeys(str(name) for name in ((run.get("details") or {}).get("missing_tools") or [])
                                          if str(name).strip()))
        if not requirements:
            return False
        if not any(all(tooling.machine_has(node, name) for name in requirements)
                   for node in node_rows.values()):
            return False
    return True


def _recover_disabled(ctx, account, role: str, state: dict, runs, window: int, percent: int) -> bool:
    """Restore a role immediately when its score/cause is corrected, or after its probation streak succeeds."""
    score, _ = failure_score(runs, window)
    if score is None or score * 100 <= percent:
        switch_on(ctx, account.id, role, "the corrected run history is now below the failure threshold")
        return True
    if _tool_missing_cause_fixed(ctx, account, runs, window):
        switch_on(ctx, account.id, role, "the missing tool is available on a machine assigned to this account")
        return True
    if _streak(ctx.db, account.id, role, state["disabled_at"]) >= RECOVERY_STREAK:
        switch_on(ctx, account.id, role, f"{RECOVERY_STREAK} successful probation tickets")
        return True
    return False


def switch_off(ctx, account_id: str, role: str, score: float, window: int, history, percent: int) -> None:
    items = _scored_window(history, window)
    diagnostics = _diagnostic_detail(items, window, score)
    note = (f"Role {role} is switched off for {account_id}: failure score {_percent(score)} over the last {window} "
            f"scored runs is above {percent}%. Its other roles stay on. {diagnostics}\n"
            "One easy probation ticket may run at a time; two in a row that succeed bring the role back.")
    ctx.db.run("INSERT INTO role_health (account_id, role, enabled, note, score, disabled_at) VALUES (%s,%s,false,%s,%s,now())"
               " ON CONFLICT (account_id, role) DO UPDATE SET enabled=false, note=EXCLUDED.note, score=EXCLUDED.score,"
               " disabled_at=now(), updated_at=now()", (account_id, role, note, round(score, 4)))
    ctx.events.emit("role.disabled", account=account_id, step=role, outcome="disabled", reason=note,
                    score=round(score, 4))
    _record_role_incident(ctx, f"{account_id}:{role}",
                          f"{account_id} switched off for role {role} (failure score {_percent(score)})", note)


def switch_on(ctx, account_id: str, role: str, reason: str = "recovery checks passed") -> None:
    ctx.db.run("UPDATE role_health SET enabled=true, note='', score=NULL, disabled_at=NULL, scoring_from=now(),"
               " updated_at=now() WHERE account_id=%s AND role=%s", (account_id, role))
    ctx.events.emit("role.enabled", account=account_id, step=role, outcome="enabled",
                    reason=f"role {role} is back on for {account_id}: {reason}")
    close_incident(ctx, "role_health", f"{account_id}:{role}",
                   f"{account_id} is back on role {role}: {reason}")


def _open_role_wide_incident(ctx, role: str, accounts: list, window: int, percent: int, histories) -> None:
    combined = []
    failures_by_account = []
    for account in accounts:
        items = _scored_window(histories.get((account.id, role), []), window)
        combined.extend(items)
        score = sum(item.failure for _, item in items) / len(items) if items else 0
        failures_by_account.append(f"{account.id} {_percent(score)}")
    counts, _ = _failure_breakdown(combined)
    cause = max(FAILURE_KINDS, key=lambda kind: counts[kind])
    detail = (f"Every eligible account for role {role} is above the {percent}% failure threshold; AI Flow kept all "
              f"{len(accounts)} accounts enabled to preserve work capacity. Most frequent failure kind: {cause}.\n"
              f"Account scores: {', '.join(failures_by_account)}\n"
              + _diagnostic_detail(combined, window))
    _record_role_incident(ctx, role,
                          f"All eligible {role} accounts score poorly; keeping them on (most frequent cause: {cause})",
                          detail)


def evaluate(ctx, account_id: str = "") -> None:
    """Re-score whole role pools so one result cannot disable too many peers; settle probation tickets."""
    with _EVALUATION_LOCK:
        with ctx.db.pool.connection() as connection:
            connection.execute("SELECT pg_advisory_lock(%s, %s)", _EVALUATION_ADVISORY_LOCK)
            try:
                _evaluate(ctx)
            finally:
                connection.execute("SELECT pg_advisory_unlock(%s, %s)", _EVALUATION_ADVISORY_LOCK)


def _evaluate(ctx) -> None:
    """Run one serialized role health sweep after acquiring the process and database locks."""
    # Pool capacity and the all-accounts incident require every peer's history, even when called after one account's run.
    _resolve_probation(ctx)
    settings = get_settings(ctx.db)
    window, percent = settings["window_runs"], settings["failure_percent"]
    history = _history(ctx.db)
    states = _states(ctx.db)
    repo_accounts = ctx.repo().accounts
    role_names = sorted({role for account in repo_accounts for role in account_roles(account)})
    for role in role_names:
        # Only accounts that can take work now count against the role's switch-off capacity.
        holders = [account for account in repo_accounts if role in account_roles(account)]
        accounts = _ready_role_accounts(ctx, holders)
        if not accounts:
            close_incident(ctx, "role_health", role, "no unpaused account is currently assigned to this role")
            continue

        full_scores = {account.id: failure_score(history.get((account.id, role), []), window)[0]
                       for account in accounts}
        all_bad = all(score is not None and score * 100 > percent for score in full_scores.values())
        if all_bad:
            # There is no healthier account to move work to. Recover any prior switches and report one pooled cause.
            for account in accounts:
                state = states.get((account.id, role))
                if state and not state["enabled"]:
                    switch_on(ctx, account.id, role, "all eligible accounts are failing, so the role pool stays enabled")
            _open_role_wide_incident(ctx, role, accounts, window, percent, history)
            continue
        close_incident(ctx, "role_health", role, "not every eligible account is above the failure threshold")

        for account in accounts:
            state = states.get((account.id, role))
            if state and not state["enabled"]:
                _recover_disabled(ctx, account, role, state, history.get((account.id, role), []), window, percent)

        states = _states(ctx.db)
        limit = _role_capacity(len(accounts))
        disabled = [account for account in accounts
                    if (state := states.get((account.id, role))) and not state["enabled"]]
        # Heal legacy state that violates today's capacity rule before evaluating new switches.
        if len(disabled) > limit:
            for account in sorted(disabled, key=lambda item: (float(states[(item.id, role)].get("score") or 0), item.id))[:len(disabled) - limit]:
                switch_on(ctx, account.id, role, "restored to keep at least two accounts ready and within the half-pool limit")
            states = _states(ctx.db)
            disabled = [account for account in accounts
                        if (state := states.get((account.id, role))) and not state["enabled"]]

        candidates = []
        for account in accounts:
            state = states.get((account.id, role))
            if state and not state["enabled"]:
                continue
            runs = history.get((account.id, role), [])
            score, _ = failure_score(runs, window, state["scoring_from"] if state else None)
            if score is not None and score * 100 > percent:
                scored = [(run, result) for run, result in runs
                          if result is not None and result.failure is not None
                          and (not state or state["scoring_from"] is None or run["started_at"] >= state["scoring_from"])]
                candidates.append((score, account, scored))
        slots = max(0, limit - len(disabled))
        for score, account, scored in sorted(candidates, key=lambda item: (-item[0], item[1].id))[:slots]:
            switch_off(ctx, account.id, role, score, window, scored, percent)
            states[(account.id, role)] = {"enabled": False, "score": score}


# --- what the Command Center shows -----------------------------------------------------------------------------
def summary(ctx) -> dict[str, Any]:
    """One row per AI account and role: switch state, success rate, cost per finished ticket, rework rounds."""
    settings = get_settings(ctx.db)
    window = settings["window_runs"]
    history = _history(ctx.db)
    states = _states(ctx.db)
    probation = {}
    for p in ctx.db.all("SELECT * FROM role_probation ORDER BY assigned_at"):
        probation.setdefault((p["account_id"], p["role"]), []).append(p)
    cutoff = datetime.now(timezone.utc) - timedelta(days=METRICS_DAYS)
    rows = []
    for account in ctx.repo().accounts:
        for role in account_roles(account):
            state = states.get((account.id, role))
            runs = history.get((account.id, role), [])
            scoring_from = state["scoring_from"] if state else None
            scored = [s for r, s in runs if s is not None and s.failure is not None
                      and (scoring_from is None or r["started_at"] >= scoring_from)][:window]
            rate = round(100 * (1 - sum(s.failure for s in scored) / len(scored)), 1) if scored else None
            recent = [(r, s) for r, s in runs if r["started_at"] >= cutoff]
            finished = {r["ticket_key"] for r, s in recent if s is not None and s.failure == 0.0}
            cost = sum(float(r["cost_usd"]) for r, _ in recent)
            enabled = state["enabled"] if state else True
            tickets = probation.get((account.id, role), [])
            since = state["disabled_at"] if state and not enabled else None
            open_ticket = next((p["ticket_key"] for p in tickets if p["result"] == ""), "")
            rows.append({
                "account": account.id, "provider": account.provider, "role": role, "enabled": enabled,
                "mode": "on" if enabled else ("probation" if probation_slot_open(ctx.db, account.id, role) else "off"),
                "note": state["note"] if state and not enabled else "",
                "disabled_at": since.isoformat() if since else None,
                "failure_score": None if not scored else round(sum(s.failure for s in scored) / len(scored), 4),
                "scored_runs": len(scored), "success_rate": rate,
                "finished_tickets": len(finished),
                "cost_per_finished_ticket": round(cost / len(finished), 4) if finished else None,
                "rework_rounds": sum(1 for _, s in recent if s is not None and s.rework),
                "probation": {"open_ticket": open_ticket, "streak": _streak(ctx.db, account.id, role, since) if since else 0,
                              "needed": RECOVERY_STREAK,
                              "results": [p["result"] for p in tickets if p["result"] and
                                          (since is None or p["assigned_at"] >= since)][-RECOVERY_STREAK:]}
                if not enabled else None,
            })
    return {"settings": settings, "metrics_days": METRICS_DAYS, "roles": rows}
