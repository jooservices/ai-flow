"""Run queries and persistence for work claims."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..db import J


BACKOFF_SQL = """
WITH last AS (
  SELECT ticket_key, outcome, ended_at,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
), streak AS (
  SELECT l.ticket_key, count(*) AS fails, max(l.ended_at) AS last_end
  FROM last l
  WHERE l.outcome IN ('failed', 'timeout', 'lost', 'no_changes')
    AND NOT EXISTS (SELECT 1 FROM last g WHERE g.ticket_key = l.ticket_key AND g.n < l.n
                    AND g.outcome NOT IN ('failed', 'timeout', 'lost', 'no_changes'))
  GROUP BY l.ticket_key
)
SELECT ticket_key, fails FROM streak
WHERE last_end > now() - make_interval(mins => LEAST(30, (2 ^ LEAST(fails, 5))::int))
"""
INTAKE_BACKOFF_SQL = """
SELECT DISTINCT ON (ticket_key) ticket_key, outcome, ended_at FROM runs
WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
ORDER BY ticket_key, started_at DESC
"""

VERIFY_RETRY_BACKOFF_SQL = """
WITH recent AS (
  SELECT ticket_key, outcome, ended_at,
         coalesce(details->>'no_verdict' = 'true', false) AS no_verdict,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = 'verify' AND status <> 'running' AND started_at > now() - interval '1 day'
    AND (%(ticket_key)s::text IS NULL OR ticket_key = %(ticket_key)s::text)
    AND started_at > coalesce((SELECT h.resolved_at FROM ticket_handoffs h
                               WHERE h.ticket_key = runs.ticket_key AND h.state = 'resolved'), '-infinity'::timestamptz)
), streak AS (
  SELECT l.ticket_key, count(*) AS fails, max(l.ended_at) AS last_end
  FROM recent l
  WHERE l.outcome IN ('failed', 'timeout', 'lost', 'no_changes') AND NOT l.no_verdict
    AND NOT EXISTS (SELECT 1 FROM recent g WHERE g.ticket_key = l.ticket_key AND g.n < l.n
                    AND (g.no_verdict OR g.outcome NOT IN ('failed', 'timeout', 'lost', 'no_changes')))
  GROUP BY l.ticket_key
)
SELECT ticket_key, fails FROM streak
WHERE last_end > now() - make_interval(mins => LEAST(30, (2 ^ LEAST(fails, 5))::int))
"""

VERIFY_TICKET_HISTORY_SQL = """
SELECT ticket_key, account_id, coalesce(details->>'no_verdict' = 'true', false) AS no_verdict, ended_at
FROM runs r
WHERE r.ticket_key = ANY(%s) AND r.step = 'verify' AND r.status <> 'running'
  AND r.started_at > coalesce((
      SELECT max(d.started_at) FROM runs d
      WHERE d.ticket_key = r.ticket_key AND d.step = 'dev' AND d.outcome = 'done'
  ), '-infinity'::timestamptz)
ORDER BY started_at DESC
"""


@dataclass
class VerifyTicketHistory:
    """Completed verifier results and the latest no-verdict end time for one ticket."""

    by_account: dict[str, list[bool]] = field(default_factory=dict)
    last_no_verdict_ended_at: datetime | None = None


def running_by_project(db) -> dict[str, int]:
    return {str(row["project"]): int(row["count"]) for row in db.all(
        "SELECT project,count(*) AS count FROM runs WHERE status='running' GROUP BY project")}


def backoff_tickets(db, step: str) -> set[str]:
    return {str(row["ticket_key"]) for row in db.all(BACKOFF_SQL, {"step": step})}


def intake_backoff(db, step: str) -> list[dict]:
    return db.all(INTAKE_BACKOFF_SQL, {"step": step})


def verify_retry_backoff(db, ticket_key: str | None = None) -> set[str]:
    """Verify failures cooling down after ordinary errors; no-verdict runs bypass this delay."""
    return {str(row["ticket_key"]) for row in db.all(VERIFY_RETRY_BACKOFF_SQL, {"ticket_key": ticket_key})}


def verify_ticket_history(db, ticket_keys: list[str]) -> dict[str, VerifyTicketHistory]:
    """Newest-first no-verdict markers for the current development round, and the latest no-verdict end time.

    A finished `dev` run with outcome `done` starts a new round and drops earlier verify history for candidate
    selection, so a barred account can try again after rework lands a new development submission.
    """
    history: dict[str, VerifyTicketHistory] = {}
    if not ticket_keys:
        return history
    for row in db.all(VERIFY_TICKET_HISTORY_SQL, (ticket_keys,)):
        ticket = history.setdefault(row["ticket_key"], VerifyTicketHistory())
        no_verdict = bool(row["no_verdict"])
        ticket.by_account.setdefault(row["account_id"], []).append(no_verdict)
        ended_at = row.get("ended_at")
        if no_verdict and ended_at and (ticket.last_no_verdict_ended_at is None
                                        or ended_at > ticket.last_no_verdict_ended_at):
            ticket.last_no_verdict_ended_at = ended_at
    return history


def save_claim_details(db, run_id: str, details: dict) -> None:
    db.run("UPDATE runs SET details=%s WHERE id=%s", (J(details), run_id))


def inherit_development_pull_request(db, run_id: str, prior: dict) -> None:
    db.run("UPDATE runs SET pr_url=%s, branch=%s, head_sha=%s, details=details || %s WHERE id=%s",
           (prior["pr_url"], prior["branch"], prior.get("head_sha") or "", J({"pr_url_source": "inherited"}), run_id))


def latest_rework(db, ticket: str) -> dict | None:
    return db.one("SELECT summary,details,started_at FROM runs WHERE ticket_key=%s AND step IN ('verify','qa')"
                  " AND outcome='rework' ORDER BY started_at DESC LIMIT 1", (ticket,))


def latest_answered_triage(db, ticket: str) -> dict | None:
    return db.one("SELECT summary,details,started_at FROM runs WHERE ticket_key=%s AND step='triage'"
                  " AND outcome='answered' ORDER BY started_at DESC LIMIT 1", (ticket,))


def development_after(db, ticket: str, started_at) -> bool:
    return bool(db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND step='dev' AND started_at > %s"
                       " AND outcome IN ('done','blocked') LIMIT 1", (ticket, started_at)))


def latest_blocked_development_before(db, ticket: str, started_at) -> dict | None:
    return db.one("SELECT summary FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='blocked'"
                  " AND started_at < %s ORDER BY started_at DESC LIMIT 1", (ticket, started_at))
