"""Verification runs that end without a verdict (docs/v4/verify-no-verdict.md).

A verify run whose answer has no `VERDICT:` line is stored with `details.no_verdict = true`. This module decides
what happens next, by rule and without AI:

* The ticket is retried at once (no backoff) on an account that has not ended this ticket without a verdict. The
  same account is asked again only when no other eligible account has still to try.
* An account with two consecutive no-verdict results on the same ticket is not offered that ticket again, and
  role health counts a no-verdict result against the verify role only from that second result on.
* When every eligible account is barred the watchdog raises one `verify_no_verdict` incident for the ticket.
* The Dashboard lists tickets waiting for verification longer than 30 minutes with the accounts that tried.

All of it is scoped to the current development round: a newer finished dev run starts the count again.
"""
from __future__ import annotations

from . import role_health, shared_accounts
from .incidents import close_incident, open_incident

KIND = "verify_no_verdict"
SLOW_MINUTES = 30
CONSECUTIVE = 2

# A failed verify run waits before the next try (the shared backoff), except a no-verdict run, which is retried
# on another account at once.
VERIFY_BACKOFF_SQL = """
WITH last AS (
  SELECT ticket_key, outcome, ended_at,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
    AND NOT coalesce(details->>'no_verdict' = 'true', false)
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

_ROUND_SQL = """
SELECT account_id, outcome, started_at, ended_at, coalesce(details->>'no_verdict' = 'true', false) AS no_verdict
FROM runs WHERE ticket_key = %s AND step = 'verify' AND status <> 'running'
  AND started_at > coalesce((SELECT max(started_at) FROM runs WHERE ticket_key = %s AND step = 'dev'
                             AND outcome = 'done'), '-infinity'::timestamptz)
ORDER BY started_at DESC
"""


def is_epic(ticket: dict) -> bool:
    return str(ticket.get("type") or "").strip().lower() == "epic"


def history(db, ticket_key: str) -> dict[str, list[bool]]:
    """Per account, newest first: did each verify run of this development round end without a verdict."""
    out: dict[str, list[bool]] = {}
    for row in db.all(_ROUND_SQL, (ticket_key, ticket_key)):
        out.setdefault(row["account_id"], []).append(bool(row["no_verdict"]))
    return out


def barred(runs: list[bool]) -> bool:
    """Two consecutive no-verdict results (newest first) bar the account from the ticket."""
    return len(runs) >= CONSECUTIVE and all(runs[:CONSECUTIVE])


def pool(ctx, ticket: dict, dev_account: str) -> list[str]:
    """Accounts that may verify the ticket at all: the verify role, not paused, not the author, label rules."""
    labels = set(ticket.get("labels") or [])
    out = []
    for acc in ctx.repo().accounts:
        if acc.paused or acc.id == dev_account or "verify" not in role_health.account_roles(acc):
            continue
        if acc.explicit_only and f"ai-{acc.provider}" not in labels:
            continue
        if role_health.modes(ctx.db, acc.id, ["verify"])["verify"] == "off":
            continue
        out.append(acc.id)
    return out


def may_take(ctx, ticket: dict, account_id: str, dev_account: str) -> bool:
    """Whether `account_id` may be offered the ticket's verification now."""
    runs = history(ctx.db, ticket["key"])
    if barred(runs.get(account_id, [])):
        return False
    if not any(runs.get(account_id, [])):  # never ended this ticket without a verdict
        return True
    online = shared_accounts.online_nodes(ctx.db)
    accounts = {acc.id: acc for acc in ctx.repo().accounts}
    for other in pool(ctx, ticket, dev_account):
        if other == account_id or barred(runs.get(other, [])) or any(runs.get(other, [])):
            continue
        if any(machine in online for machine in accounts[other].machines):
            return False  # an eligible account has not tried yet: it goes first
    return True


def exhausted(ctx, ticket: dict) -> list[str]:
    """The accounts that failed the ticket when every eligible account is barred, else an empty list."""
    dev = ctx.db.one("SELECT account_id FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='done'"
                     " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
    runs = history(ctx.db, ticket["key"])
    accounts = pool(ctx, ticket, (dev or {}).get("account_id", ""))
    if not accounts or not all(barred(runs.get(account, [])) for account in accounts):
        return []
    return sorted(accounts)


def check_incidents(ctx) -> None:
    """Watchdog: one incident per ticket whose eligible accounts all ended verification without a verdict."""
    open_now = set()
    for ticket in ctx.db.all("SELECT key, labels, type FROM tickets WHERE status='In Verify'"
                             " AND lower(coalesce(type,'')) <> 'epic'"):
        failed = exhausted(ctx, ticket)
        if not failed:
            continue
        open_now.add(ticket["key"])
        open_incident(ctx, KIND, ticket["key"], "warning",
                      f"{ticket['key']} cannot be verified: every eligible account ended without a verdict",
                      f"Reason: the verification of {ticket['key']} ended without a VERDICT line twice in a row on each"
                      f" eligible account ({', '.join(failed)}). None of them is offered the ticket again. Fix the"
                      " cause or verify the ticket by hand.")
    for row in ctx.db.all("SELECT subject FROM incidents WHERE kind=%s AND closed_at IS NULL", (KIND,)):
        if row["subject"] not in open_now:
            close_incident(ctx, KIND, row["subject"], "the ticket can be verified again or left verification")


def slow_verifications(db, minutes: int = SLOW_MINUTES) -> list[dict]:
    """Tickets waiting for verification longer than `minutes`, with the accounts that tried."""
    out = []
    for ticket in db.all(
            "SELECT t.key, t.project, t.summary, coalesce(d.ended_at, t.jira_updated, t.synced_at) AS since"
            " FROM tickets t LEFT JOIN LATERAL (SELECT ended_at FROM runs WHERE ticket_key=t.key AND step='dev'"
            "   AND outcome='done' ORDER BY started_at DESC LIMIT 1) d ON true"
            " WHERE t.status='In Verify' AND lower(coalesce(t.type,'')) <> 'epic'"
            " AND NOT ('aiflow-hold' = ANY(t.labels))"
            " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=t.key AND r.status='running')"
            " AND coalesce(d.ended_at, t.jira_updated, t.synced_at) <= now() - %s * interval '1 minute'"
            " ORDER BY since", (minutes,)):
        tried = []
        for account, runs in history(db, ticket["key"]).items():
            tried.append({"account": account, "runs": len(runs), "no_verdict": sum(runs), "barred": barred(runs)})
        out.append({"key": ticket["key"], "project": ticket["project"], "summary": ticket["summary"],
                    "waiting_minutes": int((_now(db) - ticket["since"]).total_seconds() // 60),
                    "accounts": sorted(tried, key=lambda item: item["account"])})
    return out


def _now(db):
    return db.one("SELECT now() AS t")["t"]
