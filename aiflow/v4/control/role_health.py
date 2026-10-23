"""Role health: quality by AI account and role (docs/v4/role-health.md).

Every finished run is scored for the role of its step: dev, verify, master (triage and the master cycle) or
intake_refiner (intake and impact). A merged run, or a finished run accepted by verification, is a success; a failed,
no-changes, blocked or system-stopped run is a failure; a run sent back by verification is half a failure. Runs that
ended because of quota, sign-in or a machine problem say nothing about the AI and are not scored.

When the latest `window` scored runs of one account and role have a failure score above the threshold, only that
role of that account is switched off (the account keeps its other roles), an incident alerts the owner, and the role
gets one easy probation ticket a day. Two probation tickets in a row that succeed switch the role back on, and its
scoring starts again from that moment.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .incidents import close_incident, open_incident

log = logging.getLogger("aiflow4.role_health")

DEFAULT_WINDOW = 10
DEFAULT_FAILURE_PERCENT = 60
WINDOW_LIMITS = (1, 100)
PERCENT_LIMITS = (0, 99)
RECOVERY_STREAK = 2          # probation tickets in a row that must succeed
METRICS_DAYS = 30            # cost and rework are shown for the last 30 days
HISTORY_RUNS = 500           # finished runs read per account
DEFAULT_ROLES = ("dev", "verify")
STEP_ROLE = {"dev": "dev", "verify": "verify", "triage": "master", "master": "master",
             "intake": "intake_refiner", "impact": "intake_refiner"}
# error kinds that mean the account or its machine could not work, not that the AI did bad work
UNSCORED_KINDS = {"quota", "auth", "infra", "network", "machine", "node", "tool", "tools", "missing_tool",
                  "missing-tool", "offline", "toolchain", "services"}
SUCCESS_OUTCOMES = {"merged", "human", "refined", "questions", "assessed", "answered", "escalated"}
STOPPED_OUTCOMES = {"timeout", "lost"}

RUNS_SQL = """
WITH recent AS (
  SELECT id, ticket_key, step, account_id, outcome, error_kind, summary, started_at, cost_usd,
         row_number() OVER (PARTITION BY account_id ORDER BY started_at DESC) AS n
  FROM runs
  WHERE status <> 'running' AND outcome <> '' AND step = ANY(%(steps)s::text[])
    AND (%(account)s::text = '' OR account_id = %(account)s::text)
)
SELECT r.*, CASE WHEN r.step = 'dev' AND r.outcome = 'done' THEN
  (SELECT v.outcome FROM runs v WHERE v.ticket_key = r.ticket_key AND v.step IN ('verify', 'qa')
     AND v.started_at > r.started_at AND v.outcome IN ('merged', 'human', 'rework')
   ORDER BY v.started_at LIMIT 1) END AS decision
FROM recent r WHERE r.n <= %(limit)s ORDER BY r.started_at DESC
"""


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
    if outcome == "quota" or kind in UNSCORED_KINDS:
        return Scored("excluded", None)
    if outcome == "merged":
        return Scored("merged", 0.0)
    if outcome == "done" and run["step"] == "dev":
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
    if outcome in STOPPED_OUTCOMES:
        return Scored("stopped", 1.0)
    if outcome == "blocked" or (outcome == "failed" and (run.get("summary") or "").lower().startswith("no changes")):
        return Scored("no_changes", 1.0)
    if outcome == "failed":
        return Scored("failed", 1.0)
    return Scored("excluded", None)


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
    for run in db.all(RUNS_SQL, {"steps": list(STEP_ROLE), "account": account_id, "limit": HISTORY_RUNS}):
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
    """Per role of one account: on, probation (switched off, today's probation ticket is still free) or off."""
    result = {role: "on" for role in roles}
    for row in db.all("SELECT role FROM role_health WHERE account_id=%s AND NOT enabled", (account_id,)):
        if row["role"] in result:
            result[row["role"]] = "probation" if probation_slot_open(db, account_id, row["role"]) else "off"
    return result


def _day_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def probation_slot_open(db, account_id: str, role: str) -> bool:
    if db.one("SELECT 1 AS x FROM role_probation WHERE account_id=%s AND role=%s AND result='' LIMIT 1",
              (account_id, role)):
        return False
    state = db.one("SELECT probation_unlimited FROM role_health WHERE account_id=%s AND role=%s", (account_id, role))
    if state and state.get("probation_unlimited"):
        return True
    return not db.one("SELECT 1 AS x FROM role_probation WHERE account_id=%s AND role=%s AND assigned_at >= %s",
                      (account_id, role, _day_start()))


def assign_probation(db, account_id: str, role: str, ticket_key: str) -> int | None:
    """Take today's probation slot; None when another claim already took it."""
    row = db.one("INSERT INTO role_probation (account_id, role, ticket_key) SELECT %s,%s,%s"
                 " WHERE NOT EXISTS (SELECT 1 FROM role_probation WHERE account_id=%s AND role=%s AND result='')"
                 " AND (coalesce((SELECT probation_unlimited FROM role_health WHERE account_id=%s AND role=%s),false)"
                 " OR NOT EXISTS (SELECT 1 FROM role_probation WHERE account_id=%s AND role=%s AND assigned_at >= %s))"
                 " RETURNING id", (account_id, role, ticket_key, account_id, role, account_id, role,
                                   account_id, role, _day_start()))
    if row:
        db.run("UPDATE role_health SET probation_unlimited=false WHERE account_id=%s AND role=%s",
               (account_id, role))
    return row["id"] if row else None


def expedite_probation(db, account_id: str, role: str) -> bool:
    """Open an additional probation slot after an account or machine fault is repaired."""
    row = db.one("UPDATE role_health SET probation_unlimited=true,updated_at=now()"
                 " WHERE account_id=%s AND role=%s AND NOT enabled AND NOT probation_unlimited"
                 " AND NOT EXISTS (SELECT 1 FROM role_probation WHERE account_id=%s AND role=%s AND result='')"
                 " RETURNING account_id", (account_id, role, account_id, role))
    return bool(row)


def attach_probation_run(db, probation_id: int, run_id: str) -> None:
    db.run("UPDATE role_probation SET run_id=%s WHERE id=%s", (run_id, probation_id))


def drop_probation(db, probation_id: int) -> None:
    db.run("DELETE FROM role_probation WHERE id=%s AND result=''", (probation_id,))


def _resolve_probation(ctx, account_id: str = "") -> None:
    """Record the result of open probation tickets once their run is scored; a run that ended on quota or a machine
    problem (or never ran) gives the slot back."""
    for p in ctx.db.all("SELECT * FROM role_probation WHERE result='' AND (%s='' OR account_id=%s)", (account_id, account_id)):
        run = ctx.db.one("SELECT id, ticket_key, step, account_id, outcome, error_kind, summary, status, started_at,"
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


def switch_off(ctx, account_id: str, role: str, score: float, window: int, history, percent: int) -> None:
    runs = [s for _, s in history if s is not None and s.failure is not None][:window]
    failed = sum(1 for s in runs if s.failure == 1.0)
    sent_back = sum(1 for s in runs if s.failure == 0.5)
    note = (f"Role {role} is switched off for {account_id}: failure score {_percent(score)} over the last {window} "
            f"scored runs ({failed} failed, {sent_back} sent back) is above {percent}%. Its other roles stay on. "
            f"One easy probation ticket a day; two in a row that succeed bring the role back.")
    ctx.db.run("INSERT INTO role_health (account_id, role, enabled, note, score, disabled_at) VALUES (%s,%s,false,%s,%s,now())"
               " ON CONFLICT (account_id, role) DO UPDATE SET enabled=false, probation_unlimited=false,"
               " note=EXCLUDED.note, score=EXCLUDED.score,"
               " disabled_at=now(), updated_at=now()", (account_id, role, note, round(score, 4)))
    ctx.events.emit("role.disabled", account=account_id, step=role, outcome="disabled", reason=note,
                    score=round(score, 4))
    open_incident(ctx, "role_health", f"{account_id}:{role}", "warning",
                  f"{account_id} switched off for role {role} (failure score {_percent(score)})", note)


def switch_on(ctx, account_id: str, role: str) -> None:
    ctx.db.run("UPDATE role_health SET enabled=true, note='', score=NULL, disabled_at=NULL, probation_unlimited=false, scoring_from=now(),"
               " updated_at=now() WHERE account_id=%s AND role=%s", (account_id, role))
    ctx.events.emit("role.enabled", account=account_id, step=role, outcome="enabled",
                    reason=f"role {role} is back on for {account_id} after {RECOVERY_STREAK} successful probation tickets")
    close_incident(ctx, "role_health", f"{account_id}:{role}",
                   f"{account_id} is back on role {role} after {RECOVERY_STREAK} successful probation tickets")


def evaluate(ctx, account_id: str = "") -> None:
    """Re-score every account and role (or one account): switch roles off and back on, settle probation tickets."""
    _resolve_probation(ctx, account_id)
    settings = get_settings(ctx.db)
    window, percent = settings["window_runs"], settings["failure_percent"]
    history = _history(ctx.db, account_id)
    states = _states(ctx.db)
    for account in ctx.repo().accounts:
        if account_id and account.id != account_id:
            continue
        for role in account_roles(account):
            state = states.get((account.id, role))
            if state and not state["enabled"]:
                if _streak(ctx.db, account.id, role, state["disabled_at"]) >= RECOVERY_STREAK:
                    switch_on(ctx, account.id, role)
                continue
            runs = history.get((account.id, role), [])
            score, _ = failure_score(runs, window, state["scoring_from"] if state else None)
            if score is not None and score * 100 > percent:
                switch_off(ctx, account.id, role, score, window, [(r, s) for r, s in runs
                           if not state or r["started_at"] >= state["scoring_from"]], percent)


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
