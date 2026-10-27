"""Persistence queries for run history and role-health scoring."""
from __future__ import annotations


ROLE_HEALTH_RUNS_SQL = """
WITH recent AS (
  SELECT id, ticket_key, step, account_id, outcome, error_kind, summary, details, pr_url, node, started_at, cost_usd,
         row_number() OVER (PARTITION BY account_id ORDER BY started_at DESC) AS n
  FROM runs
  WHERE status <> 'running' AND outcome <> '' AND step = ANY(%(steps)s::text[])
    AND (%(account)s::text = '' OR account_id = %(account)s::text)
)
SELECT r.*, CASE WHEN r.step = 'dev' AND r.outcome = 'done' THEN
  (SELECT v.outcome FROM runs v WHERE v.ticket_key = r.ticket_key AND v.step IN ('verify', 'qa')
     AND v.started_at > r.started_at AND v.outcome IN ('merged', 'human', 'rework')
   ORDER BY v.started_at LIMIT 1) END AS decision,
  CASE WHEN r.step = 'verify' THEN coalesce(
  (SELECT p.details->>'no_verdict' = 'true' FROM runs p WHERE p.ticket_key = r.ticket_key
     AND p.account_id = r.account_id AND p.step = 'verify' AND p.status <> 'running' AND p.started_at < r.started_at
   ORDER BY p.started_at DESC LIMIT 1), false) END AS prev_no_verdict
FROM recent r WHERE r.n <= %(limit)s ORDER BY r.started_at DESC
"""

TICKET_STUCK_STREAKS_SQL = """
WITH recent AS (
  SELECT ticket_key, outcome, details, ended_at,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
), streak AS (
  SELECT l.ticket_key, count(*) AS fails, max(l.ended_at) AS last_end
  FROM recent l
  WHERE l.outcome IN ('failed', 'timeout', 'lost', 'no_changes')
    AND coalesce(l.details->>'no_verdict' = 'true', false) = false
    AND NOT EXISTS (SELECT 1 FROM recent g WHERE g.ticket_key = l.ticket_key AND g.n < l.n
                    AND (g.outcome NOT IN ('failed', 'timeout', 'lost', 'no_changes')
                         OR coalesce(g.details->>'no_verdict' = 'true', false)))
  GROUP BY l.ticket_key
)
SELECT ticket_key, fails FROM streak
WHERE last_end > now() - make_interval(mins => LEAST(30, (2 ^ LEAST(fails, 5))::int))
"""

_BACKOFF_SQL = """
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

_INTAKE_BACKOFF_SQL = """
WITH latest AS (
  SELECT ticket_key, outcome, ended_at,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
)
SELECT ticket_key FROM latest
WHERE n = 1 AND outcome IN ('failed', 'timeout', 'lost')
  AND ended_at > now() - interval '10 minutes'
"""

_CAPACITY_HISTORY_SQL = (
    "SELECT t.key,t.project,t.type,t.labels,t.summary,t.description,t.fields,"
    "sum(greatest(0,r.tokens_in)+greatest(0,r.tokens_out))::bigint AS tokens,"
    "sum(greatest(0,extract(epoch FROM r.ended_at-r.started_at)))::float8 AS duration_s,"
    "bool_or(r.outcome='rework') AS reworked "
    "FROM runs r JOIN tickets t ON t.key=r.ticket_key "
    "WHERE r.step IN ('dev','verify') AND r.status='finished' AND r.ended_at IS NOT NULL "
    "AND (%s OR t.project=ANY(%s)) GROUP BY t.key,t.project,t.type,t.labels,t.summary,t.description,t.fields "
    "HAVING count(*) FILTER (WHERE r.tokens_in>0 OR r.tokens_out>0 OR r.ended_at>r.started_at)>0"
)

_CAPACITY_DECISIONS_SQL = """
SELECT DISTINCT ON (r.ticket_key) r.ticket_key,t.project,t.summary,r.account_id,r.provider,r.started_at,
  r.details->'capacity'->'estimate' AS estimate,r.details->'capacity'->'assignment_reason' AS reason
FROM runs r JOIN tickets t ON t.key=r.ticket_key
WHERE r.step='dev' AND r.details->'capacity' ? 'estimate' AND (%s OR t.project=ANY(%s))
ORDER BY r.ticket_key,r.started_at DESC LIMIT 100
"""

_CAPACITY_ESTIMATE_ROWS_SQL = """
SELECT DISTINCT ON (r.ticket_key) r.ticket_key,t.project,t.summary,r.started_at,
  r.details->'capacity'->'estimate' AS estimate
FROM runs r JOIN tickets t ON t.key=r.ticket_key
WHERE r.step='dev' AND r.details->'capacity' ? 'estimate' AND t.status IN ('Done','Closed')
  AND (%s OR t.project=ANY(%s)) ORDER BY r.ticket_key,r.started_at LIMIT 100
"""

_CAPACITY_ACTUAL_SQL = """
SELECT coalesce(sum(greatest(0,tokens_in)+greatest(0,tokens_out)),0)::bigint AS tokens,
  coalesce(sum(greatest(0,extract(epoch FROM ended_at-started_at))),0)::float8 AS duration_s,
  bool_or(outcome='rework') AS reworked FROM runs
WHERE ticket_key=%s AND step IN ('dev','verify') AND status='finished' AND ended_at IS NOT NULL AND started_at >= %s
"""


class RunsRepository:
    def __init__(self, db) -> None:
        self.db = db

    def role_health_history(self, steps: list[str], account: str = "", limit: int = 500) -> list[dict]:
        """Finished runs with the prior ticket-and-account verdict marker needed by role-health policy."""
        return self.db.all(ROLE_HEALTH_RUNS_SQL, {"steps": steps, "account": account, "limit": limit})

    def ticket_stuck_streaks(self, step: str) -> list[dict]:
        """Recent generic failure streaks; no-verdict verification runs break rather than extend the streak."""
        return self.db.all(TICKET_STUCK_STREAKS_SQL, {"step": step})

    def latest_development(self, ticket_key: str, delivered_only: bool = False) -> dict | None:
        """Latest finished development run, optionally limited to one that delivered work."""
        delivered = " AND outcome='done'" if delivered_only else ""
        return self.db.one("SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'"
                           + delivered + " ORDER BY started_at DESC LIMIT 1", (ticket_key,))

    def latest_master_cycle(self) -> dict | None:
        """Latest master run used to decide whether provider failover should wait one cycle."""
        return self.db.one("SELECT id,fence,ticket_key,provider,status,outcome,ended_at FROM runs"
                           " WHERE step='master' ORDER BY started_at DESC LIMIT 1")

    def latest_verify_for_ticket(self, ticket_key: str) -> dict | None:
        return self.db.one("SELECT summary,details FROM runs WHERE ticket_key=%s AND step='verify' "
                           "AND status<>'running' ORDER BY started_at DESC LIMIT 1", (ticket_key,))

    def latest_triage_for_ticket(self, ticket_key: str) -> dict | None:
        return self.db.one("SELECT started_at FROM runs WHERE ticket_key=%s AND step='triage' "
                           "ORDER BY started_at DESC LIMIT 1", (ticket_key,))

    def latest_pull_request_for_ticket(self, ticket_key: str) -> dict | None:
        return self.db.one("SELECT pr_url FROM runs WHERE ticket_key=%s AND pr_url<>'' "
                           "ORDER BY started_at DESC LIMIT 1", (ticket_key,))

    def latest_rework_for_ticket(self, ticket_key: str) -> dict | None:
        return self.db.one("SELECT summary,details,started_at FROM runs WHERE ticket_key=%s "
                           "AND step IN ('verify','qa') AND outcome='rework' ORDER BY started_at DESC LIMIT 1",
                           (ticket_key,))

    def latest_answered_triage(self, ticket_key: str) -> dict | None:
        return self.db.one("SELECT summary,details,started_at FROM runs WHERE ticket_key=%s AND step='triage' "
                           "AND outcome='answered' ORDER BY started_at DESC LIMIT 1", (ticket_key,))

    def has_completed_development_after(self, ticket_key: str, started_at) -> bool:
        return bool(self.db.one("SELECT 1 AS found FROM runs WHERE ticket_key=%s AND step='dev' "
                                "AND started_at>%s AND outcome IN ('done','blocked') LIMIT 1",
                                (ticket_key, started_at)))

    def latest_blocked_development_before(self, ticket_key: str, started_at) -> dict | None:
        return self.db.one("SELECT summary FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='blocked' "
                           "AND started_at<%s ORDER BY started_at DESC LIMIT 1", (ticket_key, started_at))

    def save_claim_details(self, run_id: str, details) -> None:
        from ..db import J
        self.db.run("UPDATE runs SET details=%s WHERE id=%s", (J(details), run_id))

    def inherit_pull_request(self, run_id: str, prior: dict) -> None:
        from ..db import J
        self.db.run("UPDATE runs SET pr_url=%s,branch=%s,head_sha=%s,details=details || %s WHERE id=%s",
                    (prior["pr_url"], prior["branch"], prior.get("head_sha") or "",
                     J({"pr_url_source": "inherited"}), run_id))

    def ticket_detail_rows(self, ticket_key: str) -> list[dict]:
        """Recent run details shown in the ticket drawer."""
        return self.db.all("SELECT id,step,account_id,provider,model,node,fence,status,outcome,error_kind,"
                           "started_at,ended_at,tokens_in,tokens_out,cost_usd,branch,pr_url,head_sha,summary "
                           "FROM runs WHERE ticket_key=%s ORDER BY started_at DESC LIMIT 20", (ticket_key,))

    def latest_capacity_decision(self, ticket_key: str) -> dict | None:
        """Latest development assignment estimate shown in the ticket drawer."""
        return self.db.one(
            "SELECT account_id,provider,started_at,details->'capacity'->'estimate' AS estimate,"
            "details->'capacity'->'assignment_reason' AS reason,details->'capacity'->'account_fit' AS account_fit "
            "FROM runs WHERE ticket_key=%s AND step='dev' AND details->'capacity' ? 'estimate' "
            "ORDER BY started_at DESC LIMIT 1", (ticket_key,))

    def timeline_rows(self, ticket_key: str) -> list[dict]:
        """Run intervals used to calculate the ticket timeline."""
        return self.db.all("SELECT id,step,account_id,status,outcome,started_at,ended_at,heartbeat_at FROM runs"
                           " WHERE ticket_key=%s ORDER BY started_at", (ticket_key,))

    def cooling_tickets(self, step: str) -> set[str]:
        """Ticket keys waiting out the shared exponential backoff for a failed step."""
        return {row["ticket_key"] for row in self.db.all(_BACKOFF_SQL, {"step": step})}

    def intake_waiting_tickets(self, step: str) -> set[str]:
        """Intake or impact tickets whose latest failed attempt is still inside its ten-minute wait."""
        return {row["ticket_key"] for row in self.db.all(_INTAKE_BACKOFF_SQL, {"step": step})}

    def running_counts_by_project(self) -> dict[str, int]:
        """Number of active runs in each project for candidate capacity checks."""
        return {row["project"]: int(row["count"]) for row in self.db.all(
            "SELECT project,count(*) AS count FROM runs WHERE status='running' GROUP BY project")}

    def capacity_history(self, project_keys: list[str] | tuple[str, ...] | None = None) -> list[dict]:
        """Completed ticket-level work samples used by quota-aware capacity estimates."""
        return self.db.all(_CAPACITY_HISTORY_SQL, (project_keys is None, list(project_keys or ())))

    def capacity_decisions(self, project_keys: list[str] | tuple[str, ...] | None = None) -> list[dict]:
        """Latest recorded development assignment explanations for capacity planning."""
        return self.db.all(_CAPACITY_DECISIONS_SQL, (project_keys is None, list(project_keys or ())))

    def capacity_estimate_rows(self, project_keys: list[str] | tuple[str, ...] | None = None) -> list[dict]:
        """Completed ticket estimates used to compare estimates with actual work."""
        return self.db.all(_CAPACITY_ESTIMATE_ROWS_SQL, (project_keys is None, list(project_keys or ())))

    def capacity_actual(self, ticket_key: str, started_at) -> dict | None:
        """Aggregate completed development and verification work after an estimate was recorded."""
        return self.db.one(_CAPACITY_ACTUAL_SQL, (ticket_key, started_at))

    def dashboard_busy_by_node(self) -> dict[str, int]:
        """Running session counts used by each node's registry summary."""
        return {row["node"]: int(row["busy"]) for row in self.db.all(
            "SELECT node,count(*) AS busy FROM runs WHERE status='running' GROUP BY node")}

    def dashboard_running_rows(self) -> list[dict]:
        """All running rows with ticket summaries for account and live-run summaries."""
        return self.db.all("SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                           " WHERE r.status='running' ORDER BY r.started_at")

    def dashboard_recent_rows(self) -> list[dict]:
        """Latest finished rows with ticket summaries for the Dashboard run list."""
        return self.db.all("SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                           " WHERE r.status<>'running' ORDER BY r.started_at DESC LIMIT 30")

    def dashboard_daily_usage(self) -> dict[str, dict]:
        """Token and cost totals since database midnight, grouped by account."""
        return {row["account_id"]: row for row in self.db.all(
            "SELECT account_id,coalesce(sum(tokens_in),0) AS tokens_in,coalesce(sum(tokens_out),0) AS tokens_out,"
            "coalesce(sum(cost_usd),0) AS cost_usd FROM runs WHERE started_at >= date_trunc('day',now()) "
            "GROUP BY account_id")}

    def dashboard_already_done_tickets(self) -> list[dict]:
        """Latest successful development rows used to explain already-delivered work."""
        return self.db.all("SELECT DISTINCT ON (ticket_key) id,ticket_key,pr_url,details FROM runs"
                           " WHERE step='dev' AND outcome='done' ORDER BY ticket_key,started_at DESC")

    def dashboard_totals(self, projects: list[str] | None = None) -> dict:
        """Last-day usage totals, optionally limited to visible projects."""
        columns = ("count(*) AS runs, coalesce(sum(cost_usd),0) AS cost, "
                   "coalesce(sum(tokens_in),0) AS tin, coalesce(sum(tokens_out),0) AS tout, "
                   "count(*) FILTER (WHERE outcome='done') AS done, "
                   "count(*) FILTER (WHERE outcome IN ('failed','blocked','timeout','lost')) AS failed")
        if projects is None:
            return self.db.one(f"SELECT {columns} FROM runs WHERE started_at > now() - interval '24 hours'")
        return self.db.one(
            "SELECT count(*) FILTER (WHERE started_at > now()-interval '24 hours') AS runs_24h,"
            " coalesce(sum(cost_usd) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS cost_24h,"
            " coalesce(sum(tokens_in) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_in_24h,"
            " coalesce(sum(tokens_out) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_out_24h,"
            " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome='done') AS done_24h,"
            " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome = ANY(%s)) AS failed_24h"
            " FROM runs WHERE project=ANY(%s)", (["failed", "blocked", "timeout", "lost"], projects))
