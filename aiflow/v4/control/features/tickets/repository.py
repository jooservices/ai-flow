"""Named SQL queries for the ticket mirror and ticket-keyed control views."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import Ticket


def ticket_repository_of(ctx: Any = None, *, db: Any = None, fallback: bool = True
                         ) -> "TicketRepository | FakeTicketRepository | None":
    """Return the context's shared repository, adapting legacy db-only callers."""
    repository = getattr(ctx, "ticket_repository", None) if ctx is not None else None
    if repository is not None or not fallback:
        return repository
    return TicketRepository(db if db is not None else ctx.db)


DEV_TICKET_SQL = (
    " AND NOT ('aiflow-hold' = ANY(labels))"
    " AND NOT ('aiflow-split-parent' = ANY(labels))"
    " AND ((status = 'To Do' AND 'aiflow-ready' = ANY(labels))"
    "      OR (status IN ('To Do', 'Backlog')"
    "          AND EXISTS (SELECT 1 FROM unnest(labels) AS label"
    "                      WHERE starts_with(label, 'aiflow-split-') AND label <> 'aiflow-split-parent')))")


def _labels_without(labels: list[str], label: str) -> list[str]:
    return [value for value in labels if value != label]


class TicketRepository:
    """Own named SQL queries for the ticket aggregate and its ticket-keyed views."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def _one(self, name: str, sql: str, params: Any = ()) -> dict | None:
        return self.db.one(sql, params)

    def _all(self, name: str, sql: str, params: Any = ()) -> list[dict]:
        return self.db.all(sql, params)

    def _run(self, name: str, sql: str, params: Any = ()) -> int:
        return self.db.run(sql, params)

    def _execute(self, name: str, sql: str, params: Any = ()) -> Any:
        return self.db.execute(sql, params)

    def get(self, key: str) -> Ticket | None:
        row = self._one("get", "SELECT key,project,status,type,priority,summary,description,labels,parent_key,blocked_by"
                        " FROM tickets WHERE key=%s", (key,))
        return Ticket.from_row(row) if row else None

    def update_status(self, key: str, status: str) -> None:
        self._run("update_status", "UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s", (status, key))

    def remove_label(self, key: str, label: str) -> None:
        self._run("remove_label", "UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (label, key))

    def update_description(self, key: str, description: str) -> None:
        self._run("update_description", "UPDATE tickets SET description=%s, synced_at=now() WHERE key=%s",
                  (description, key))

    def epics(self, project: str) -> list[dict]:
        return self._all("epics", "SELECT key, summary, status FROM tickets WHERE project=%s AND type='Epic'"
                         " ORDER BY key", (project,))

    def change_requests(self, project: str, label: str) -> list[dict]:
        return self._all("change_requests", "SELECT key, status, summary, parent_key, jira_created FROM tickets"
                         " WHERE project=%s AND %s = ANY(labels) ORDER BY jira_created DESC NULLS LAST, key DESC",
                         (project, label))

    def epic_in_project(self, epic: str, project: str) -> dict | None:
        return self._one("epic_in_project", "SELECT key FROM tickets WHERE key=%s AND project=%s AND type='Epic'",
                         (epic, project))

    def open_project_tickets(self, project: str, exclude: str) -> list[dict]:
        return self._all("open_project_tickets", "SELECT key, type, status, summary, parent_key FROM tickets"
                         " WHERE project=%s AND status<>'Done' AND key<>%s ORDER BY type<>'Epic', key LIMIT 300",
                         (project, exclude))

    def ticket_scope_filter(self, project: str, scope: tuple[str, ...] | None) -> tuple[str, tuple]:
        if project:
            return " AND ticket IN (SELECT key FROM tickets WHERE project=%s)", (project,)
        if scope is not None:
            return " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))", (list(scope),)
        return "", ()

    def human_review_thread_filters(self, project: str, scope: tuple[str, ...] | None
                                    ) -> tuple[str, tuple, str, tuple]:
        return self.human_review_thread_filters_for(project, scope)

    @staticmethod
    def human_review_thread_filters_for(project: str, scope: tuple[str, ...] | None
                                        ) -> tuple[str, tuple, str, tuple]:
        event_filter, event_params = TicketRepository.ticket_scope_filter_for(project, scope)
        if project:
            return event_filter, event_params, " AND project=%s", (project,)
        if scope is not None:
            return event_filter, event_params, " AND project=ANY(%s)", (list(scope),)
        return event_filter, event_params, "", ()

    @staticmethod
    def ticket_scope_filter_for(project: str, scope: tuple[str, ...] | None) -> tuple[str, tuple]:
        if project:
            return " AND ticket IN (SELECT key FROM tickets WHERE project=%s)", (project,)
        if scope is not None:
            return " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))", (list(scope),)
        return "", ()

    def review_metric_tickets(self, cohort_start: Any, project_filter: str,
                              project_params: tuple) -> list[dict]:
        sql = ("SELECT key, project, status, labels, blocked_by, done_at, jira_updated, synced_at FROM tickets"
               " WHERE (coalesce(done_at,jira_updated,synced_at)>=%s"
               " OR status IN ('Human review','Needs owner') OR cardinality(blocked_by)>0)" + project_filter)
        return self._all("review_metric_tickets", sql, (cohort_start,) + project_params)

    def review_queue_candidates(self, project_filter: str, project_scope: tuple[str, ...] | None) -> list[dict]:
        if project_filter:
            where, params = " AND project=%s", (project_filter,)
        elif project_scope is not None:
            where, params = " AND project=ANY(%s)", (list(project_scope),)
        else:
            where, params = "", ()
        sql = ("SELECT key, status, blocked_by FROM tickets WHERE (status IN ('Human review','Needs owner') OR"
               " (cardinality(blocked_by)>0 AND status NOT IN ('Done','Closed')))" + where + " ORDER BY project,key")
        return self._all("review_queue_candidates", sql, params)

    def finished_without_person(self, scope: tuple[str, ...] | None, start: Any, end: Any) -> list[dict]:
        project_filter = " AND t.project=ANY(%s)" if scope is not None else ""
        scope_params: tuple = (list(scope),) if scope is not None else ()
        sql = ("SELECT DISTINCT t.key, t.summary, t.done_at FROM tickets t WHERE t.status='Done'" + project_filter
               + " AND t.done_at >= %s AND t.done_at <= %s AND EXISTS (SELECT 1 FROM runs r"
               " WHERE r.ticket_key=t.key AND r.status<>'running' AND lower(r.outcome) IN ('done','pass','passed')"
               " AND r.started_at <= t.done_at AND NOT EXISTS (SELECT 1 FROM runs later"
               " WHERE later.ticket_key=t.key AND later.started_at > r.started_at AND later.started_at <= t.done_at))"
               " AND NOT EXISTS (SELECT 1 FROM write_audit w WHERE w.ticket=t.key AND w.action='transition'"
               " AND w.payload->>'source'='human-review' AND w.result='Done' AND w.ts <= t.done_at)")
        return self._all("finished_without_person", sql, scope_params + (start, end))

    def needed_a_person(self, scope: tuple[str, ...] | None, start: Any, end: Any) -> list[dict]:
        ticket_filter = " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))" if scope is not None else ""
        scope_params: tuple = (list(scope),) if scope is not None else ()
        sql = ("SELECT observation_key, ticket, reason, options, sent_at FROM master_escalations"
               " WHERE sent_at >= %s AND sent_at <= %s" + ticket_filter + " ORDER BY sent_at DESC")
        return self._all("needed_a_person", sql, (start, end) + scope_params)

    def repeated_failure_events(self, scope: tuple[str, ...] | None, start: Any, end: Any) -> list[dict]:
        ticket_filter = " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))" if scope is not None else ""
        scope_params: tuple = (list(scope),) if scope is not None else ()
        sql = ("SELECT ts, type FROM events WHERE type IN ('flow_bug.created','flow_bug.repeated')"
               " AND ts >= %s AND ts <= %s" + ticket_filter)
        return self._all("repeated_failure_events", sql, (start, end) + scope_params)

    def current_repeated_failures(self, scope: tuple[str, ...] | None, week_start: Any) -> list[dict]:
        project_filter = " AND t.project=ANY(%s)" if scope is not None else ""
        scope_params: tuple = (list(scope),) if scope is not None else ()
        sql = ("SELECT f.ticket_key, f.signature, f.repeat_count, f.updated_at, t.summary, t.status"
               " FROM flow_bug_tracker f LEFT JOIN tickets t ON t.key=f.ticket_key"
               " WHERE f.updated_at >= %s" + project_filter + " ORDER BY f.updated_at DESC LIMIT 50")
        return self._all("current_repeated_failures", sql, (week_start,) + scope_params)

    def capacity_history(self, project_keys: list[str] | tuple[str, ...] | None = None) -> list[dict]:
        project_filter = (project_keys is None, list(project_keys or ()))
        sql = ("SELECT t.key,t.project,t.type,t.labels,t.summary,t.description,t.fields,"
               "sum(greatest(0,r.tokens_in)+greatest(0,r.tokens_out))::bigint AS tokens,"
               "sum(greatest(0,extract(epoch FROM r.ended_at-r.started_at)))::float8 AS duration_s,"
               "bool_or(r.outcome='rework') AS reworked FROM runs r JOIN tickets t ON t.key=r.ticket_key "
               "WHERE r.step IN ('dev','verify') AND r.status='finished' AND r.ended_at IS NOT NULL "
               "AND (%s OR t.project=ANY(%s)) GROUP BY t.key,t.project,t.type,t.labels,t.summary,t.description,t.fields "
               "HAVING count(*) FILTER (WHERE r.tokens_in>0 OR r.tokens_out>0 OR r.ended_at>r.started_at)>0")
        return self._all("capacity_history", sql, project_filter)

    def capacity_ready_tickets(self, project_filter: tuple) -> list[dict]:
        sql = ("SELECT key,project,type,priority,labels,summary,description,fields FROM tickets "
               "WHERE status='To Do' AND 'aiflow-ready'=ANY(labels) AND NOT ('aiflow-hold'=ANY(labels)) "
               "AND (%s OR project=ANY(%s)) ORDER BY priority,key")
        return self._all("capacity_ready_tickets", sql, project_filter)

    def capacity_decisions(self, project_filter: tuple) -> list[dict]:
        sql = ("SELECT DISTINCT ON (r.ticket_key) r.ticket_key,t.project,t.summary,r.account_id,r.provider,r.started_at,"
               "r.details->'capacity'->'estimate' AS estimate,r.details->'capacity'->'assignment_reason' AS reason "
               "FROM runs r JOIN tickets t ON t.key=r.ticket_key "
               "WHERE r.step='dev' AND r.details->'capacity' ? 'estimate' "
               "AND (%s OR t.project=ANY(%s)) ORDER BY r.ticket_key,r.started_at DESC LIMIT 100")
        return self._all("capacity_decisions", sql, project_filter)

    def capacity_estimate_rows(self, project_filter: tuple) -> list[dict]:
        sql = ("SELECT DISTINCT ON (r.ticket_key) r.ticket_key,t.project,t.summary,r.started_at,"
               "r.details->'capacity'->'estimate' AS estimate FROM runs r JOIN tickets t ON t.key=r.ticket_key "
               "WHERE r.step='dev' AND r.details->'capacity' ? 'estimate' AND t.status IN ('Done','Closed') "
               "AND (%s OR t.project=ANY(%s)) ORDER BY r.ticket_key,r.started_at LIMIT 100")
        return self._all("capacity_estimate_rows", sql, project_filter)

    def version_links(self, ids: list[str]) -> list[dict]:
        return self._all("version_links", "SELECT version_id, ticket_key FROM ticket_versions"
                         " WHERE version_id = ANY(%s)", (ids,))

    def progress_tickets(self, keys: list[str]) -> list[dict]:
        return self._all("progress_tickets", "SELECT key, status, type, labels, summary, blocked_by,"
                         " first_in_progress_at FROM tickets WHERE key = ANY(%s)", (keys,))

    def readiness_tickets(self, keys: list[str]) -> list[dict]:
        return self._all("readiness_tickets", "SELECT key, status, type, labels, summary, blocked_by"
                         " FROM tickets WHERE key = ANY(%s)", (keys,))

    def blocker_tickets(self, keys: list[str]) -> list[dict]:
        return self._all("blocker_tickets", "SELECT key, status, type, labels, summary, blocked_by"
                         " FROM tickets WHERE key = ANY(%s)", (keys,))

    def active_run_rows(self) -> list[dict]:
        return self._all("active_run_rows", "SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                         " WHERE r.status='running' ORDER BY r.started_at")

    def recent_run_rows(self) -> list[dict]:
        return self._all("recent_run_rows", "SELECT r.*, t.summary FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                         " WHERE r.status<>'running' ORDER BY r.started_at DESC LIMIT 30")

    def snapshot_tickets(self) -> list[dict]:
        return self._all("snapshot_tickets", "SELECT key, project, status, type, priority, labels, summary, parent_key,"
                         " blocked_by, done_at, jira_updated FROM tickets ORDER BY project, key")

    def timeline_ticket(self, key: str) -> dict | None:
        return self._one("timeline_ticket", "SELECT key, status, labels, done_at FROM tickets WHERE key=%s", (key,))

    def ticket_ancestor(self, key: str) -> dict | None:
        return self._one("ticket_ancestor", "SELECT key,type,parent_key FROM tickets WHERE key=%s", (key,))

    def ticket_detail(self, key: str) -> dict | None:
        return self._one("ticket_detail", "SELECT * FROM tickets WHERE key=%s", (key,))

    def verify_candidates(self, projects: list[str], ticket_key: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'In Verify'"
               " AND NOT ('aiflow-hold' = ANY(labels))")
        params = (projects,)
        if ticket_key is not None:
            sql += " AND key=%s"
            params += (ticket_key,)
        return self._all("verify_candidates", sql, params)

    def workable_tickets(self, older_than_min: float = 0.0) -> list[dict]:
        sql = ("SELECT key, project, status, blocked_by FROM tickets WHERE NOT ('aiflow-hold' = ANY(labels))"
               " AND ((status='To Do' AND 'aiflow-ready' = ANY(labels)) OR status='In Verify')"
               " AND coalesce(jira_updated, synced_at) <= now() - %s * interval '1 minute'")
        return self._all("workable_tickets", sql, (older_than_min,))

    def idle_tickets(self, older_than_min: float = 0.0) -> list[dict]:
        sql = ("SELECT key,project,status,blocked_by FROM tickets WHERE NOT ('aiflow-hold'=ANY(labels))"
               " AND type <> 'Epic' AND ((status='To Do' AND 'aiflow-ready'=ANY(labels)) OR status='In Verify')"
               " AND coalesce(jira_updated,synced_at) <= now() - %s * interval '1 minute'")
        return self._all("idle_tickets", sql, (older_than_min,))

    def triage_candidates(self, projects: list[str], ticket_key: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Needs owner'"
               " AND NOT ('owner-escalated' = ANY(labels)) AND NOT ('aiflow-hold' = ANY(labels))"
               " AND EXISTS (SELECT 1 FROM escalations e WHERE e.ticket_key = tickets.key"
               " AND e.resolution = 'open')")
        params = (projects,)
        if ticket_key is not None:
            sql += " AND tickets.key=%s"
            params += (ticket_key,)
        return self._all("triage_candidates", sql, params)

    def intake_candidates(self, projects: list[str], ticket_key: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Intake'"
               " AND NOT ('aiflow-hold' = ANY(labels)) AND NOT ('aiflow-split-parent' = ANY(labels))")
        params = (projects,)
        if ticket_key is not None:
            sql += " AND key=%s"
            params += (ticket_key,)
        sql += " ORDER BY jira_created NULLS LAST, key"
        return self._all("intake_candidates", sql, params)

    def impact_candidates(self, projects: list[str], ticket_key: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Backlog'"
               " AND 'change-request' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))"
               " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = tickets.key"
               " AND r.step = 'impact' AND r.outcome = 'assessed')")
        params = (projects,)
        if ticket_key is not None:
            sql += " AND tickets.key=%s"
            params += (ticket_key,)
        sql += " ORDER BY jira_created NULLS LAST, key"
        return self._all("impact_candidates", sql, params)

    def developer_candidates(self, projects: list[str], ticket_key: str | None = None) -> list[dict]:
        sql = "SELECT * FROM tickets WHERE project = ANY(%s)" + DEV_TICKET_SQL
        params = (projects,)
        if ticket_key is not None:
            sql += " AND key=%s"
            params += (ticket_key,)
        return self._all("developer_candidates", sql, params)

    def hotfile_past_files(self, ticket_key: str, epic: str, limit: int) -> list[dict]:
        return self._all("hotfile_past_files", "SELECT r.details->'changed_files' AS files FROM runs r"
                         " JOIN tickets t ON t.key = r.ticket_key WHERE r.step = 'dev' AND r.outcome = 'done'"
                         " AND r.details ? 'changed_files' AND (r.ticket_key = %s OR (%s <> '' AND t.parent_key = %s))"
                         " ORDER BY r.started_at DESC LIMIT %s", (ticket_key, epic, epic, limit))

    def hotfile_epic(self, key: str) -> dict | None:
        return self._one("hotfile_epic", "SELECT summary, description FROM tickets WHERE key=%s", (key,))

    def hotfile_in_flight(self, exclude: str) -> list[dict]:
        return self._all("hotfile_in_flight", "SELECT * FROM tickets WHERE key <> %s"
                         " AND (status IN ('In Verify', 'Human review', 'Ready to merge')"
                         " OR EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = tickets.key"
                         " AND r.step = 'dev' AND r.status = 'running'))", (exclude,))

    def cleanup_hot_file_reservations(self, connection: Any, grace_s: int) -> None:
        connection.execute("DELETE FROM hot_file_reservations h WHERE h.reserved_at < now() - %s * interval '1 second'"
                           " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.id::text = h.run_id AND r.status = 'running')"
                           " AND NOT EXISTS (SELECT 1 FROM tickets t WHERE t.key = h.ticket_key AND ("
                           " t.status IN ('In Verify', 'Human review', 'Ready to merge'))) ", (grace_s,))

    def knowledge_ticket_ancestor(self, key: str) -> dict | None:
        return self._one("knowledge_ticket_ancestor", "SELECT type,parent_key FROM tickets WHERE key=%s", (key,))

    def knowledge_capture_run(self, run_id: str) -> dict | None:
        return self._one("knowledge_capture_run", "SELECT r.*,t.summary AS ticket_summary,t.description AS ticket_description,"
                         "t.parent_key FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key"
                         " WHERE r.id=%s AND r.status='finished'", (run_id,))

    def knowledge_closed_tickets(self, ticket_key: str = "") -> list[dict]:
        sql = ("SELECT t.key,t.project,t.summary,t.description,t.parent_key FROM tickets t WHERE t.status='Done' "
               "AND NOT EXISTS (SELECT 1 FROM knowledge_items k WHERE k.kind='study_case' "
               "AND k.source_type='ticket' AND k.source_ref=t.key) "
               "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=t.key AND r.status='running')")
        params: tuple = ()
        if ticket_key:
            sql += " AND t.key=%s"
            params = (ticket_key,)
        return self._all("knowledge_closed_tickets", sql, params)

    def memory_index_tickets(self, max_text: int) -> list[dict]:
        sql = ("SELECT project, 'ticket' AS kind, key AS ref, key AS ticket, "
               "left(key || ': ' || summary || E'\\n\\n' || description, %s) AS text, "
               "coalesce(jira_created, synced_at) AS created_at FROM tickets")
        return self._all("memory_index_tickets", sql, (max_text,))

    def memory_index_run_summaries(self, max_text: int) -> list[dict]:
        sql = ("SELECT r.project, 'run_summary' AS kind, r.id::text AS ref, r.ticket_key AS ticket, "
               "left(r.ticket_key || ': ' || coalesce(t.summary, '') || E'\\n' || r.step || ' ' "
               "|| coalesce(nullif(r.outcome, ''), 'finished') || ': ' || r.summary, %s) AS text, "
               "coalesce(r.ended_at, r.started_at) AS created_at FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key "
               "WHERE r.status = 'finished' AND r.summary <> ''")
        return self._all("memory_index_run_summaries", sql, (max_text,))

    def memory_index_findings(self, max_text: int) -> list[dict]:
        sql = ("SELECT r.project, 'finding' AS kind, r.id::text AS ref, r.ticket_key AS ticket, "
               "left(r.ticket_key || ': ' || coalesce(t.summary, '') || E'\\nverify findings:\\n' "
               "|| coalesce(nullif(r.details->>'findings', ''), r.summary), %s) AS text, "
               "coalesce(r.ended_at, r.started_at) AS created_at FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key "
               "WHERE r.step = 'verify' AND r.status = 'finished' AND r.outcome = 'rework' "
               "AND coalesce(nullif(r.details->>'findings', ''), r.summary) <> ''")
        return self._all("memory_index_findings", sql, (max_text,))

    def disagreements(self, after_seconds: int) -> list[dict]:
        sql = ("SELECT d.ticket AS key, t.project, t.summary, d.jira_status, d.github_state, d.expected, d.detail, d.since,"
               " t.pr_url, extract(epoch FROM (now()-d.since))::int AS age_s FROM ticket_disagreements d"
               " JOIN tickets t ON t.key=d.ticket WHERE d.since < now() - make_interval(secs => %s) ORDER BY d.since")
        return self._all("disagreements", sql, (after_seconds,))

    def upsert_disagreement(self, params: Any = ()) -> Any:
        sql = ("INSERT INTO ticket_disagreements (ticket, jira_status, github_state, expected, detail)"
               " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ticket) DO UPDATE SET jira_status=EXCLUDED.jira_status,"
               " github_state=EXCLUDED.github_state, expected=EXCLUDED.expected, detail=EXCLUDED.detail, updated_at=now()")
        return self._run("upsert_disagreement", sql, params)

    def delete_disagreement(self, params: Any = ()) -> Any:
        return self._run("delete_disagreement", "DELETE FROM ticket_disagreements WHERE ticket=%s", params)

    def record_ticket_version_added(self, params: Any = ()) -> Any:
        sql = ("INSERT INTO ticket_version_added (ticket_key, version_id, added_at) VALUES (%s,%s,%s)"
               " ON CONFLICT (ticket_key, version_id) DO UPDATE SET added_at=EXCLUDED.added_at")
        return self._run("record_ticket_version_added", sql, params)

    def ticket_version_added_for_versions(self, params: Any = ()) -> Any:
        return self._all("ticket_version_added_for_versions",
                         "SELECT ticket_key, version_id, added_at FROM ticket_version_added WHERE version_id = ANY(%s)",
                         params)

    def licensing_usage_report(self) -> list[dict]:
        sql = ("WITH run_usage AS (SELECT project, date_trunc('month', started_at AT TIME ZONE 'UTC') AS month_at,"
               " coalesce(sum(tokens_in),0) AS tokens_in, coalesce(sum(tokens_out),0) AS tokens_out,"
               " coalesce(sum(cost_usd),0)::numeric(12,6) AS cost_usd FROM runs"
               " GROUP BY project, date_trunc('month', started_at AT TIME ZONE 'UTC')), finished AS ("
               " SELECT project, date_trunc('month', done_at AT TIME ZONE 'UTC') AS month_at,"
               " count(*)::int AS tickets_finished FROM tickets WHERE status='Done' AND done_at IS NOT NULL"
               " GROUP BY project, date_trunc('month', done_at AT TIME ZONE 'UTC'))"
               " SELECT coalesce(r.project,f.project) AS project,"
               " to_char(coalesce(r.month_at,f.month_at),'YYYY-MM') AS month,"
               " coalesce(f.tickets_finished,0)::int AS tickets_finished, coalesce(r.tokens_in,0) AS tokens_in,"
               " coalesce(r.tokens_out,0) AS tokens_out, coalesce(r.cost_usd,0)::numeric(12,6) AS cost_usd"
               " FROM run_usage r FULL OUTER JOIN finished f USING (project,month_at) ORDER BY month DESC, project")
        return self._all("licensing_usage_report", sql)

    def scale_history_tickets(self, cursor: Any, rows: list[tuple]) -> None:
        cursor.executemany("INSERT INTO tickets (key, project, status, labels, summary, description, priority, jira_created,"
                           " jira_updated, synced_at, done_at) VALUES (%s,%s,%s,%s,%s,%s,'Medium',%s,%s,%s,%s)", rows)

    def scale_wave_tickets(self, cursor: Any, rows: list[tuple]) -> None:
        cursor.executemany("INSERT INTO tickets (key, project, status, labels, summary, description, priority, blocked_by,"
                           " jira_created) VALUES (%s,%s,'To Do',%s,%s,%s,%s,%s,%s)", rows)

    def scale_mark_done(self, key: str) -> int:
        return self._run("scale_mark_done", "UPDATE tickets SET status='Done' WHERE key=%s", (key,))

    def scale_restore_todo(self, key: str) -> int:
        return self._run("scale_restore_todo", "UPDATE tickets SET status='To Do' WHERE key=%s AND status='Done'", (key,))


    NAMED_QUERIES = (
        "get",
        "update_status",
        "remove_label",
        "update_description",
        "epics",
        "change_requests",
        "epic_in_project",
        "open_project_tickets",
        "review_metric_tickets",
        "review_queue_candidates",
        "finished_without_person",
        "needed_a_person",
        "repeated_failure_events",
        "current_repeated_failures",
        "capacity_history",
        "capacity_ready_tickets",
        "capacity_decisions",
        "capacity_estimate_rows",
        "version_links",
        "progress_tickets",
        "readiness_tickets",
        "blocker_tickets",
        "active_run_rows",
        "recent_run_rows",
        "snapshot_tickets",
        "timeline_ticket",
        "ticket_ancestor",
        "ticket_detail",
        "verify_candidates",
        "triage_candidates",
        "intake_candidates",
        "impact_candidates",
        "developer_candidates",
        "hotfile_past_files",
        "hotfile_epic",
        "hotfile_in_flight",
        "cleanup_hot_file_reservations",
        "knowledge_ticket_ancestor",
        "knowledge_capture_run",
        "knowledge_closed_tickets",
        "memory_index_tickets",
        "memory_index_run_summaries",
        "memory_index_findings",
        "disagreements",
        "upsert_disagreement",
        "delete_disagreement",
        "record_ticket_version_added",
        "ticket_version_added_for_versions",
        "workable_tickets",
        "idle_tickets",
        "licensing_usage_report",
        "scale_history_tickets",
        "scale_wave_tickets",
        "scale_mark_done",
        "scale_restore_todo",
        "upsert_created_bug_ticket",
        "list_needs_owner_tickets",
        "mark_ticket_intake",
        "update_ticket_status_synced",
        "get_ticket_type_and_parent",
        "get_ticket_project",
        "remove_label_from_ticket",
        "append_label_if_missing",
        "get_review_queue_ticket",
        "get_ticket_status",
        "update_status_preserving_done_at",
        "upsert_story_ticket",
        "get_ticket_in_project",
        "get_epic_key_in_project",
        "upsert_intake_bug_ticket",
        "list_recent_dev_changes_for_project",
        "insert_bug_ticket_if_absent",
        "insert_created_task_if_absent",
        "list_pending_post_merge_checks",
        "list_pending_post_merge_checks_for_keys",
        "list_environment_reports",
        "list_tickets_outside_statuses",
        "set_status_clearing_ready_label",
        "list_open_environment_report_groups",
        "resolve_environment_reports_for_done_tickets",
        "insert_created_bug_if_absent",
        "list_ready_or_verify_tickets",
        "list_statuses_for_keys",
        "list_open_tickets_with_blockers",
        "remove_blocker_from_ticket",
        "set_ticket_status_intake",
        "list_ready_or_verify_ticket_statuses",
        "list_assignable_ready_or_verify_tickets",
        "stamp_approved_at_if_empty",
        "close_pull_request_on_ticket",
        "get_ticket_notice_fields",
        "list_idle_ready_or_verify_tickets",
        "get_ticket_project_and_parent",
        "list_done_ticket_keys",
        "list_stale_ready_or_verify_tickets",
        "list_ticket_keys_in_project",
        "get_ticket_sync_fields",
        "earliest_first_in_progress_at",
        "upsert_mirrored_ticket_row",
        "replace_ticket_version_links",
        "list_project_tickets_for_epic_reconcile",
        "update_status_with_done_timestamp",
        "set_merge_note_checks_running",
        "set_merge_note",
        "mark_pull_request_merged",
        "update_merge_facts",
        "stamp_approved_at_now",
        "set_merge_note_missing_pull_request",
        "list_merge_ready_tickets",
        "update_status_with_optional_done_at",
        "list_tickets_needing_pr_reconcile",
        "touch_pr_checked_at",
        "clear_merge_state_keep_pr_url",
        "record_merge_on_ticket",
        "append_rework_label_if_missing",
        "clear_approval_and_set_merge_note",
        "store_pr_url_and_state",
        "store_pr_url",
        "get_ticket_project_and_summary",
        "list_recent_human_attention_tickets",
        "list_version_ticket_counts",
        "list_open_tickets_for_version",
        "count_tickets_by_project",
        "list_project_tickets_for_report",
        "get_ticket_status_and_labels",
        "append_label_when_absent",
        "list_holds_missing_label",
        "list_ticket_summaries_for_keys",
        "get_epic_status_by_key",
        "list_human_review_ticket_rows",
        "update_description_synced",
        "append_label_if_absent_synced",
        "get_ticket_transition_fields",
        "transition_status_stamping_done_at",
        "get_ticket_jira_updated",
        "set_ticket_status_synced",
        "get_ticket_key_project_status",
        "list_pending_scale_proof_signatures",
        "get_ticket_bundle_fields",
        "resolve_tool_waits_for_done_tickets",
        "list_open_tool_waits",
        "list_ticket_types_in_project",
        "list_orphaned_in_progress_keys",
        "mark_ticket_todo",
        "list_done_keys_among",
        "get_full_ticket_row",
    )

    def upsert_created_bug_ticket(self, params: Any = ()) -> Any:
        """Insert or refresh a Created Bug ticket row."""
        return self._run(
            "upsert_created_bug_ticket",
            "INSERT INTO tickets (key, project, status, type, priority, labels, summary, description, jira_created, jira_updated)"
            " VALUES (%s,%s,'Created','Bug','Highest',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
            " type='Bug', priority='Highest', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
            " description=EXCLUDED.description, synced_at=now()",
            params,
        )

    def list_needs_owner_tickets(self, params: Any = ()) -> Any:
        """List Needs owner tickets with escalation flag for the master snapshot."""
        return self._all(
            "list_needs_owner_tickets",
            "SELECT key, project, summary, description, priority, labels, jira_updated, ('owner-escalated'=ANY(labels)) AS owner_escalated FROM tickets WHERE status='Needs owner' ORDER BY priority, jira_created NULLS LAST, key",
            params,
        )

    def mark_ticket_intake(self, params: Any = ()) -> Any:
        """Set a ticket status to Intake and refresh synced_at."""
        return self._run(
            "mark_ticket_intake",
            "UPDATE tickets SET status='Intake', synced_at=now() WHERE key=%s",
            params,
        )

    def update_ticket_status_synced(self, params: Any = ()) -> Any:
        """Set ticket status and refresh synced_at."""
        return self._run(
            "update_ticket_status_synced",
            'UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s',
            params,
        )

    def get_ticket_type_and_parent(self, params: Any = ()) -> Any:
        """Return key, type and parent_key for one ticket."""
        return self._one(
            "get_ticket_type_and_parent",
            'SELECT key,type,parent_key FROM tickets WHERE key=%s',
            params,
        )

    def get_ticket_project(self, params: Any = ()) -> Any:
        """Return the project key for one ticket."""
        return self._one(
            "get_ticket_project",
            'SELECT project FROM tickets WHERE key=%s',
            params,
        )

    def remove_label_from_ticket(self, params: Any = ()) -> Any:
        """Remove one label from a ticket's labels array."""
        return self._run(
            "remove_label_from_ticket",
            'UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s',
            params,
        )

    def append_label_if_missing(self, params: Any = ()) -> Any:
        """Append a label when it is not already present."""
        return self._run(
            "append_label_if_missing",
            'UPDATE tickets SET labels=CASE WHEN %s=ANY(labels) THEN labels ELSE array_append(labels,%s) END WHERE key=%s',
            params,
        )

    def get_review_queue_ticket(self, params: Any = ()) -> Any:
        """Load review fields for one ticket including blockers."""
        return self._one(
            "get_review_queue_ticket",
            'SELECT key, project, status, labels, summary, blocked_by FROM tickets WHERE key=%s',
            params,
        )

    def get_ticket_status(self, params: Any = ()) -> Any:
        """Return the current status of one ticket."""
        return self._one(
            "get_ticket_status",
            'SELECT status FROM tickets WHERE key=%s',
            params,
        )

    def update_status_preserving_done_at(self, params: Any = ()) -> Any:
        """Update status and stamp done_at when moving to Done."""
        return self._run(
            "update_status_preserving_done_at",
            "UPDATE tickets SET status=%s, done_at=CASE WHEN %s='Done' THEN coalesce(done_at,now()) ELSE done_at END, synced_at=now() WHERE key=%s",
            params,
        )

    def upsert_story_ticket(self, params: Any = ()) -> Any:
        """Insert or refresh a Story ticket under a parent."""
        return self._run(
            "upsert_story_ticket",
            "INSERT INTO tickets (key, project, status, type, labels, summary, description, parent_key, jira_created, jira_updated) VALUES (%s,%s,%s,'Story',%s,%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET status=EXCLUDED.status, labels=EXCLUDED.labels, parent_key=EXCLUDED.parent_key, synced_at=now()",
            params,
        )

    def get_ticket_in_project(self, params: Any = ()) -> Any:
        """Return key, summary, type and parent for a ticket in a project."""
        return self._one(
            "get_ticket_in_project",
            'SELECT key, summary, type, parent_key FROM tickets WHERE key=%s AND project=%s',
            params,
        )

    def get_epic_key_in_project(self, params: Any = ()) -> Any:
        """Return the epic key when it exists in the given project."""
        return self._one(
            "get_epic_key_in_project",
            "SELECT key FROM tickets WHERE key=%s AND project=%s AND type='Epic'",
            params,
        )

    def upsert_intake_bug_ticket(self, params: Any = ()) -> Any:
        """Insert or refresh an Intake Bug ticket from UAT feedback."""
        return self._run(
            "upsert_intake_bug_ticket",
            "INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated) VALUES (%s,%s,'Intake','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET status='Intake', type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary, description=EXCLUDED.description, synced_at=now()",
            params,
        )

    def list_recent_dev_changes_for_project(self, params: Any = ()) -> Any:
        """List latest finished or merged dev runs per ticket in a project."""
        return self._all(
            "list_recent_dev_changes_for_project",
            "SELECT DISTINCT ON (t.key) t.key, t.summary, t.type, t.status, t.parent_key, p.type AS parent_type, r.branch, r.head_sha, r.summary AS change_summary, r.details, r.started_at, r.ended_at FROM runs r JOIN tickets t ON t.key=r.ticket_key LEFT JOIN tickets p ON p.key=t.parent_key WHERE r.project=%s AND t.project=%s AND r.step='dev' AND r.status<>'running' AND r.outcome IN ('done','merged') ORDER BY t.key, r.started_at DESC",
            params,
        )

    def insert_bug_ticket_if_absent(self, params: Any = ()) -> Any:
        """Insert a Bug ticket row when the key does not already exist."""
        return self._run(
            "insert_bug_ticket_if_absent",
            "INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated) VALUES (%s,%s,%s,'Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
            params,
        )

    def insert_created_task_if_absent(self, params: Any = ()) -> Any:
        """Insert a Created Task ticket when the key does not already exist."""
        return self._run(
            "insert_created_task_if_absent",
            "INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated) VALUES (%s,%s,'Created','Task',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
            params,
        )

    def list_pending_post_merge_checks(self, params: Any = ()) -> Any:
        """List pending post-merge checks for Done tickets in a project."""
        return self._all(
            "list_pending_post_merge_checks",
            "SELECT c.ticket_key, c.criteria FROM environment_post_merge_checks c JOIN tickets t ON t.key=c.ticket_key WHERE c.project=%s AND c.status='pending' AND t.status='Done' ORDER BY c.created_at, c.ticket_key",
            params,
        )

    def list_pending_post_merge_checks_for_keys(self, params: Any = ()) -> Any:
        """List pending post-merge checks for specific Done ticket keys."""
        return self._all(
            "list_pending_post_merge_checks_for_keys",
            "SELECT c.ticket_key, c.criteria FROM environment_post_merge_checks c JOIN tickets t ON t.key=c.ticket_key WHERE c.project=%s AND c.status='pending' AND c.ticket_key = ANY(%s) AND t.status='Done' ORDER BY c.created_at, c.ticket_key",
            params,
        )

    def list_environment_reports(self, params: Any = ()) -> Any:
        """List environment reports with ticket summaries, newest first."""
        return self._all(
            "list_environment_reports",
            'SELECT r.ticket_key, r.project, r.item, r.machine, r.detail, r.environment_ticket, r.created_at, r.resolved_at, r.resolution, t.summary FROM environment_reports r LEFT JOIN tickets t ON t.key = r.ticket_key ORDER BY (r.resolved_at IS NOT NULL), r.created_at DESC LIMIT %s',
            params,
        )

    def list_tickets_outside_statuses(self, params: Any = ()) -> Any:
        """List tickets whose status is not in the given set."""
        return self._all(
            "list_tickets_outside_statuses",
            'SELECT key, project, status, labels, summary, description FROM tickets WHERE NOT (status = ANY(%s)) ORDER BY project, key',
            params,
        )

    def set_status_clearing_ready_label(self, params: Any = ()) -> Any:
        """Set status, drop aiflow-ready, and refresh synced_at."""
        return self._run(
            "set_status_clearing_ready_label",
            "UPDATE tickets SET status=%s, labels=array_remove(labels,'aiflow-ready'), synced_at=now() WHERE key=%s",
            params,
        )

    def list_open_environment_report_groups(self, params: Any = ()) -> Any:
        """Group unresolved environment reports by project and item."""
        return self._all(
            "list_open_environment_report_groups",
            'SELECT project, item, min(created_at) AS since, array_agg(ticket_key ORDER BY ticket_key) AS tickets, max(environment_ticket) AS environment_ticket FROM environment_reports WHERE resolved_at IS NULL GROUP BY project, item',
            params,
        )

    def resolve_environment_reports_for_done_tickets(self, params: Any = ()) -> Any:
        """Mark open environment reports resolved when their ticket is Done."""
        return self._run(
            "resolve_environment_reports_for_done_tickets",
            "UPDATE environment_reports SET resolved_at=now(), resolution='ticket is done' WHERE resolved_at IS NULL AND ticket_key IN (SELECT key FROM tickets WHERE status='Done')",
            params,
        )

    def insert_created_bug_if_absent(self, params: Any = ()) -> Any:
        """Insert a Created Bug ticket when the key does not already exist."""
        return self._run(
            "insert_created_bug_if_absent",
            "INSERT INTO tickets (key,project,status,type,labels,summary,description,jira_created,jira_updated) VALUES (%s,%s,'Created','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
            params,
        )

    def list_ready_or_verify_tickets(self, params: Any = ()) -> Any:
        """List non-held ready To Do or In Verify tickets with blockers."""
        return self._all(
            "list_ready_or_verify_tickets",
            "SELECT key,project,status,blocked_by FROM tickets WHERE NOT ('aiflow-hold'=ANY(labels)) AND ((status='To Do' AND 'aiflow-ready'=ANY(labels)) OR status='In Verify')",
            params,
        )

    def list_statuses_for_keys(self, params: Any = ()) -> Any:
        """Return key and status for the given ticket keys."""
        return self._all(
            "list_statuses_for_keys",
            'SELECT key,status FROM tickets WHERE key=ANY(%s)',
            params,
        )

    def list_open_tickets_with_blockers(self, params: Any = ()) -> Any:
        """List non-done tickets that still declare blocked_by entries."""
        return self._all(
            "list_open_tickets_with_blockers",
            "SELECT key,blocked_by FROM tickets WHERE cardinality(blocked_by)>0 AND status NOT IN ('Done','Closed')",
            params,
        )

    def remove_blocker_from_ticket(self, params: Any = ()) -> Any:
        """Remove one key from a ticket's blocked_by array."""
        return self._run(
            "remove_blocker_from_ticket",
            'UPDATE tickets SET blocked_by=array_remove(blocked_by,%s),synced_at=now() WHERE key=%s',
            params,
        )

    def set_ticket_status_intake(self, params: Any = ()) -> Any:
        """Set a ticket status to Intake and refresh synced_at."""
        return self._run(
            "set_ticket_status_intake",
            "UPDATE tickets SET status='Intake',synced_at=now() WHERE key=%s",
            params,
        )

    def list_ready_or_verify_ticket_statuses(self, params: Any = ()) -> Any:
        """List key and status for non-held ready To Do or In Verify tickets."""
        return self._all(
            "list_ready_or_verify_ticket_statuses",
            "SELECT key,status FROM tickets WHERE NOT ('aiflow-hold'=ANY(labels)) AND ((status='To Do' AND 'aiflow-ready'=ANY(labels)) OR status='In Verify')",
            params,
        )

    def list_assignable_ready_or_verify_tickets(self, params: Any = ()) -> Any:
        """List non-held In Verify or ready To Do tickets with project."""
        return self._all(
            "list_assignable_ready_or_verify_tickets",
            "SELECT key,status,project FROM tickets WHERE (status='In Verify' OR (status='To Do' AND 'aiflow-ready'=ANY(labels))) AND NOT ('aiflow-hold'=ANY(labels))",
            params,
        )

    def stamp_approved_at_if_empty(self, params: Any = ()) -> Any:
        """Set approved_at to now when it is still null."""
        return self._run(
            "stamp_approved_at_if_empty",
            'UPDATE tickets SET approved_at=coalesce(approved_at, now()) WHERE key=%s',
            params,
        )

    def close_pull_request_on_ticket(self, params: Any = ()) -> Any:
        """Mark the ticket PR closed and store merge note."""
        return self._run(
            "close_pull_request_on_ticket",
            "UPDATE tickets SET pr_state='closed', pr_url=coalesce(nullif(%s,''),pr_url), merge_note=%s, pr_checked_at=now() WHERE key=%s",
            params,
        )

    def get_ticket_notice_fields(self, params: Any = ()) -> Any:
        """Load key, project, labels, summary and fields for a notice."""
        return self._one(
            "get_ticket_notice_fields",
            'SELECT key, project, labels, summary, fields FROM tickets WHERE key=%s',
            params,
        )

    def list_idle_ready_or_verify_tickets(self, params: Any = ()) -> Any:
        """List non-epic ready or In Verify tickets stale by jira_updated."""
        return self._all(
            "list_idle_ready_or_verify_tickets",
            "SELECT key,project,status,blocked_by FROM tickets WHERE NOT ('aiflow-hold'=ANY(labels)) AND type <> 'Epic' AND ((status='To Do' AND 'aiflow-ready'=ANY(labels)) OR status='In Verify') AND coalesce(jira_updated,synced_at) <= now() - %s * interval '1 minute'",
            params,
        )

    def get_ticket_project_and_parent(self, params: Any = ()) -> Any:
        """Return key, project and parent_key for one ticket."""
        return self._one(
            "get_ticket_project_and_parent",
            'SELECT key,project,parent_key FROM tickets WHERE key=%s',
            params,
        )

    def list_done_ticket_keys(self, params: Any = ()) -> Any:
        """Return keys of every ticket in Done status."""
        return self._all(
            "list_done_ticket_keys",
            "SELECT key FROM tickets WHERE status='Done'",
            params,
        )

    def list_stale_ready_or_verify_tickets(self, params: Any = ()) -> Any:
        """List ready or In Verify tickets stale by jira_updated with blockers."""
        return self._all(
            "list_stale_ready_or_verify_tickets",
            "SELECT key, project, status, blocked_by FROM tickets WHERE NOT ('aiflow-hold' = ANY(labels)) AND ((status='To Do' AND 'aiflow-ready' = ANY(labels)) OR status='In Verify') AND coalesce(jira_updated, synced_at) <= now() - %s * interval '1 minute'",
            params,
        )

    def list_ticket_keys_in_project(self, params: Any = ()) -> Any:
        """Return every ticket key in a project."""
        return self._all(
            "list_ticket_keys_in_project",
            'SELECT key FROM tickets WHERE project=%s',
            params,
        )

    def get_ticket_sync_fields(self, params: Any = ()) -> Any:
        """Return status, labels, parent_key and jira_updated for one ticket."""
        return self._one(
            "get_ticket_sync_fields",
            'SELECT status, labels, parent_key, jira_updated FROM tickets WHERE key=%s',
            params,
        )

    def earliest_first_in_progress_at(self, params: Any = ()) -> Any:
        """Keep the earliest first_in_progress_at timestamp for a ticket."""
        return self._run(
            "earliest_first_in_progress_at",
            'UPDATE tickets SET first_in_progress_at=LEAST(first_in_progress_at, %s) WHERE key=%s',
            params,
        )

    def upsert_mirrored_ticket_row(self, params: Any = ()) -> Any:
        """Insert or update a full mirrored ticket row from Jira."""
        return self._run(
            "upsert_mirrored_ticket_row",
            "INSERT INTO tickets (key, project, status, type, priority, labels, summary, description, parent_key, blocked_by, jira_created, jira_updated, done_at, fields, synced_at) VALUES (%(key)s,%(project)s,%(status)s,%(type)s,%(priority)s,%(labels)s, %(summary)s,%(description)s,%(parent_key)s,%(blocked_by)s,%(jira_created)s,%(jira_updated)s,%(done_at)s,%(fields)s, now()) ON CONFLICT (key) DO UPDATE SET status=EXCLUDED.status, type=EXCLUDED.type, priority=EXCLUDED.priority, labels=EXCLUDED.labels, summary=EXCLUDED.summary, description=EXCLUDED.description, parent_key=EXCLUDED.parent_key, blocked_by=EXCLUDED.blocked_by, jira_updated=EXCLUDED.jira_updated, fields=EXCLUDED.fields, done_at=CASE WHEN tickets.status <> 'Done' AND EXCLUDED.status = 'Done' THEN EXCLUDED.done_at WHEN tickets.done_at IS NULL AND EXCLUDED.status = 'Done' THEN EXCLUDED.done_at ELSE tickets.done_at END, synced_at=now()",
            params,
        )

    def replace_ticket_version_links(self, params: Any = ()) -> Any:
        """Replace fix-version links for one ticket."""
        return self._run(
            "replace_ticket_version_links",
            'WITH removed AS (DELETE FROM ticket_versions WHERE ticket_key=%s RETURNING ticket_key) INSERT INTO ticket_versions (ticket_key, version_id) SELECT %s, fix.version_id FROM unnest(%s::text[]) AS fix(version_id) CROSS JOIN (SELECT count(*) FROM removed) AS deletion_done ON CONFLICT (ticket_key, version_id) DO NOTHING',
            params,
        )

    def list_project_tickets_for_epic_reconcile(self, params: Any = ()) -> Any:
        """List project tickets with type and parent for epic reconciliation."""
        return self._all(
            "list_project_tickets_for_epic_reconcile",
            'SELECT key, project, status, type, parent_key, labels FROM tickets WHERE project=%s ORDER BY key',
            params,
        )

    def update_status_with_done_timestamp(self, params: Any = ()) -> Any:
        """Update status and set done_at when newly reaching Done."""
        return self._run(
            "update_status_with_done_timestamp",
            "UPDATE tickets SET status=%s, done_at=CASE WHEN status <> 'Done' AND %s='Done' THEN %s ELSE done_at END, synced_at=now() WHERE key=%s",
            params,
        )

    def set_merge_note_checks_running(self, params: Any = ()) -> Any:
        """Set the merge note that checks are still running."""
        return self._run(
            "set_merge_note_checks_running",
            "UPDATE tickets SET merge_note='waiting: the checks are still running' WHERE key=%s",
            params,
        )

    def set_merge_note(self, params: Any = ()) -> Any:
        """Store a merge note on a ticket."""
        return self._run(
            "set_merge_note",
            'UPDATE tickets SET merge_note=%s WHERE key=%s',
            params,
        )

    def mark_pull_request_merged(self, params: Any = ()) -> Any:
        """Set pr_state to merged for a ticket."""
        return self._run(
            "mark_pull_request_merged",
            "UPDATE tickets SET pr_state='merged' WHERE key=%s",
            params,
        )

    def update_merge_facts(self, params: Any = ()) -> Any:
        """Store PR URL, state, merge sha and related merge fields."""
        return self._run(
            "update_merge_facts",
            "UPDATE tickets SET pr_url=%s, pr_state=%s, merged_at=coalesce(%s, merged_at, now()), merge_sha=CASE WHEN %s<>'' THEN %s ELSE merge_sha END, merged_by=CASE WHEN %s<>'' THEN %s ELSE merged_by END, merge_note='', pr_checked_at=now() WHERE key=%s",
            params,
        )

    def stamp_approved_at_now(self, params: Any = ()) -> Any:
        """Set approved_at to now when it is still null."""
        return self._run(
            "stamp_approved_at_now",
            'UPDATE tickets SET approved_at=now() WHERE key=%s AND approved_at IS NULL',
            params,
        )

    def set_merge_note_missing_pull_request(self, params: Any = ()) -> Any:
        """Set the merge note that the ticket has no pull request."""
        return self._run(
            "set_merge_note_missing_pull_request",
            "UPDATE tickets SET merge_note='waiting: the ticket has no pull request' WHERE key=%s",
            params,
        )

    def list_merge_ready_tickets(self, params: Any = ()) -> Any:
        """List tickets in a status or approved Human review awaiting merge."""
        return self._all(
            "list_merge_ready_tickets",
            "SELECT * FROM tickets WHERE status=%s OR (status='Human review' AND approved_at IS NOT NULL AND merged_at IS NULL) ORDER BY approved_at NULLS FIRST, key",
            params,
        )

    def update_status_with_optional_done_at(self, params: Any = ()) -> Any:
        """Update status and optionally stamp done_at from a provided time."""
        return self._run(
            "update_status_with_optional_done_at",
            "UPDATE tickets SET status=%s, done_at=CASE WHEN %s='Done' THEN coalesce(%s, done_at, now()) ELSE done_at END, synced_at=now() WHERE key=%s",
            params,
        )

    def list_tickets_needing_pr_reconcile(self, params: Any = ()) -> Any:
        """List non-epic tickets whose PR state still needs reconciliation."""
        return self._all(
            "list_tickets_needing_pr_reconcile",
            "SELECT t.*, r.pr_url AS latest_pr FROM tickets t JOIN LATERAL ( SELECT pr_url FROM runs WHERE ticket_key=t.key AND pr_url<>'' ORDER BY started_at DESC LIMIT 1) r ON true WHERE t.type <> 'Epic' AND (t.pr_state <> 'merged' OR t.status <> 'Done' OR t.merged_at IS NULL) AND NOT (t.pr_state='closed' AND t.status='Done' AND t.pr_checked_at > now() - interval '10 minutes') ORDER BY t.pr_checked_at NULLS FIRST, t.key LIMIT %s",
            params,
        )

    def touch_pr_checked_at(self, params: Any = ()) -> Any:
        """Refresh pr_checked_at to now for a ticket."""
        return self._run(
            "touch_pr_checked_at",
            'UPDATE tickets SET pr_checked_at=now() WHERE key=%s',
            params,
        )

    def clear_merge_state_keep_pr_url(self, params: Any = ()) -> Any:
        """Clear merge fields while keeping or replacing the PR URL."""
        return self._run(
            "clear_merge_state_keep_pr_url",
            "UPDATE tickets SET pr_state='', pr_url=%s, merged_at=NULL, merge_sha='', merged_by='' WHERE key=%s",
            params,
        )

    def record_merge_on_ticket(self, params: Any = ()) -> Any:
        """Record a successful or pending merge onto the ticket row."""
        return self._run(
            "record_merge_on_ticket",
            "UPDATE tickets SET pr_url=coalesce(nullif(%s,''),pr_url), pr_state=CASE WHEN %s THEN 'merged' ELSE pr_state END, merged_at=coalesce(%s, merged_at, now()), merge_sha=%s, merged_by=%s, approved_at=coalesce(approved_at, now()), merge_note='', pr_checked_at=now() WHERE key=%s",
            params,
        )

    def append_rework_label_if_missing(self, params: Any = ()) -> Any:
        """Append the rework label when it is not already present."""
        return self._run(
            "append_rework_label_if_missing",
            "UPDATE tickets SET labels=CASE WHEN 'rework'=ANY(labels) THEN labels ELSE array_append(labels,'rework') END WHERE key=%s",
            params,
        )

    def clear_approval_and_set_merge_note(self, params: Any = ()) -> Any:
        """Clear approved_at and store a merge note."""
        return self._run(
            "clear_approval_and_set_merge_note",
            'UPDATE tickets SET approved_at=NULL, merge_note=%s WHERE key=%s',
            params,
        )

    def store_pr_url_and_state(self, params: Any = ()) -> Any:
        """Store PR URL and state and refresh pr_checked_at."""
        return self._run(
            "store_pr_url_and_state",
            'UPDATE tickets SET pr_url=%s, pr_state=%s, pr_checked_at=now() WHERE key=%s',
            params,
        )

    def store_pr_url(self, params: Any = ()) -> Any:
        """Store PR URL and refresh pr_checked_at."""
        return self._run(
            "store_pr_url",
            'UPDATE tickets SET pr_url=%s, pr_checked_at=now() WHERE key=%s',
            params,
        )

    def get_ticket_project_and_summary(self, params: Any = ()) -> Any:
        """Return project and summary for one ticket."""
        return self._one(
            "get_ticket_project_and_summary",
            'SELECT project, summary FROM tickets WHERE key=%s',
            params,
        )

    def list_recent_human_attention_tickets(self, params: Any = ()) -> Any:
        """List the ten newest Needs owner or Human review tickets."""
        return self._all(
            "list_recent_human_attention_tickets",
            "SELECT key, project, summary, status FROM tickets WHERE status IN ('Needs owner', 'Human review') ORDER BY jira_updated DESC NULLS LAST LIMIT 10",
            params,
        )

    def list_version_ticket_counts(self, params: Any = ()) -> Any:
        """Count tickets and Done tickets per version in a project."""
        return self._all(
            "list_version_ticket_counts",
            "SELECT v.id, v.name, v.released, v.release_date, count(t.key)::int AS ticket_count, count(t.key) FILTER (WHERE t.status='Done')::int AS done_count FROM versions v LEFT JOIN ticket_versions tv ON tv.version_id=v.id LEFT JOIN tickets t ON t.key=tv.ticket_key WHERE v.project=%s GROUP BY v.id, v.name, v.released, v.release_date ORDER BY v.name, v.id",
            params,
        )

    def list_open_tickets_for_version(self, params: Any = ()) -> Any:
        """List non-Done tickets linked to a version in a project."""
        return self._all(
            "list_open_tickets_for_version",
            "SELECT t.key, t.summary, t.status FROM ticket_versions tv JOIN tickets t ON t.key=tv.ticket_key WHERE tv.version_id=%s AND t.project=%s AND t.status <> 'Done' ORDER BY t.key",
            params,
        )

    def count_tickets_by_project(self, params: Any = ()) -> Any:
        """Count mirrored tickets grouped by project."""
        return self._all(
            "count_tickets_by_project",
            'SELECT project,count(*) AS count FROM tickets GROUP BY project',
            params,
        )

    def list_project_tickets_for_report(self, params: Any = ()) -> Any:
        """List report fields for every ticket in a project."""
        return self._all(
            "list_project_tickets_for_report",
            'SELECT key, project, status, type, summary, description, parent_key, labels, jira_created, jira_updated, done_at FROM tickets WHERE project=%s ORDER BY key',
            params,
        )

    def get_ticket_status_and_labels(self, params: Any = ()) -> Any:
        """Return status and labels for one ticket."""
        return self._one(
            "get_ticket_status_and_labels",
            'SELECT status, labels FROM tickets WHERE key = %s',
            params,
        )

    def append_label_when_absent(self, params: Any = ()) -> Any:
        """Append a label when the ticket does not already have it."""
        return self._run(
            "append_label_when_absent",
            'UPDATE tickets SET labels = array_append(labels, %s) WHERE key = %s AND NOT (%s = ANY(labels))',
            params,
        )

    def list_holds_missing_label(self, params: Any = ()) -> Any:
        """List open retry holds whose ticket no longer carries the hold label."""
        return self._all(
            "list_holds_missing_label",
            'SELECT h.id, h.ticket_key FROM retry_holds h LEFT JOIN tickets t ON t.key = h.ticket_key WHERE h.released_at IS NULL AND t.key IS NOT NULL AND NOT (%s = ANY(t.labels))',
            params,
        )

    def list_ticket_summaries_for_keys(self, params: Any = ()) -> Any:
        """Load key, project, summary and status for the given keys."""
        return self._all(
            "list_ticket_summaries_for_keys",
            'SELECT key,project,summary,status FROM tickets WHERE key=ANY(%s)',
            params,
        )

    def get_epic_status_by_key(self, params: Any = ()) -> Any:
        """Return key and status for one epic ticket by key."""
        return self._all(
            "get_epic_status_by_key",
            "SELECT key, status FROM tickets WHERE key=%s AND type='Epic'",
            params,
        )

    def list_human_review_ticket_rows(self, params: Any = ()) -> Any:
        """Load human-review ticket rows for the given keys."""
        return self._all(
            "list_human_review_ticket_rows",
            'SELECT key, project, type, summary, description, labels, parent_key, status, blocked_by FROM tickets WHERE key=ANY(%s) ORDER BY project,key',
            params,
        )

    def update_description_synced(self, params: Any = ()) -> Any:
        """Update description and refresh synced_at."""
        return self._run(
            "update_description_synced",
            'UPDATE tickets SET description=%s, synced_at=now() WHERE key=%s',
            params,
        )

    def append_label_if_absent_synced(self, params: Any = ()) -> Any:
        """Append a label when missing (Jira fields sync path)."""
        return self._run(
            "append_label_if_absent_synced",
            'UPDATE tickets SET labels=CASE WHEN %s = ANY(labels) THEN labels ELSE array_append(labels,%s) END WHERE key=%s',
            params,
        )

    def get_ticket_transition_fields(self, params: Any = ()) -> Any:
        """Load project, type, status, labels, summary and fields for a transition."""
        return self._one(
            "get_ticket_transition_fields",
            'SELECT project, type, status, labels, summary, fields FROM tickets WHERE key=%s',
            params,
        )

    def transition_status_stamping_done_at(self, params: Any = ()) -> Any:
        """Update status and stamp done_at when newly reaching Done."""
        return self._run(
            "transition_status_stamping_done_at",
            "UPDATE tickets SET status=%s, done_at=CASE WHEN status <> 'Done' AND %s='Done' THEN now() ELSE done_at END, synced_at=now() WHERE key=%s",
            params,
        )

    def get_ticket_jira_updated(self, params: Any = ()) -> Any:
        """Return jira_updated for one ticket."""
        return self._one(
            "get_ticket_jira_updated",
            'SELECT jira_updated FROM tickets WHERE key=%s',
            params,
        )

    def set_ticket_status_synced(self, params: Any = ()) -> Any:
        """Set ticket status and refresh synced_at."""
        return self._run(
            "set_ticket_status_synced",
            'UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s',
            params,
        )

    def get_ticket_key_project_status(self, params: Any = ()) -> Any:
        """Return key, project and status for one ticket."""
        return self._one(
            "get_ticket_key_project_status",
            'SELECT key, project, status FROM tickets WHERE key=%s',
            params,
        )

    def list_pending_scale_proof_signatures(self, params: Any = ()) -> Any:
        """List scale-proof finding signatures still pending resolution."""
        return self._all(
            "list_pending_scale_proof_signatures",
            "SELECT f.signature FROM scale_proof_findings f JOIN scale_proof_runs r ON r.id=f.last_run_id LEFT JOIN tickets t ON t.key=f.ticket WHERE NOT (f.signature=ANY(%s)) AND (COALESCE(f.ticket,'')='' OR (f.last_error<>'' AND t.status='Done')) ORDER BY f.updated_at,f.signature LIMIT %s",
            params,
        )

    def get_ticket_bundle_fields(self, params: Any = ()) -> Any:
        """Load key, project, status, type and summary for a support bundle."""
        return self._one(
            "get_ticket_bundle_fields",
            'SELECT key,project,status,type,summary FROM tickets WHERE key=%s',
            params,
        )

    def resolve_tool_waits_for_done_tickets(self, params: Any = ()) -> Any:
        """Mark open tool waits resolved when their ticket is Done."""
        return self._run(
            "resolve_tool_waits_for_done_tickets",
            "UPDATE tool_waits SET resolved_at=now(), resolution='ticket is done' WHERE resolved_at IS NULL AND ticket_key IN (SELECT key FROM tickets WHERE status='Done')",
            params,
        )

    def list_open_tool_waits(self, params: Any = ()) -> Any:
        """List unresolved tool waits with ticket summaries."""
        return self._all(
            "list_open_tool_waits",
            'SELECT w.ticket_key, w.project, w.requirement, w.machine, w.created_at, w.detail, t.summary FROM tool_waits w LEFT JOIN tickets t ON t.key = w.ticket_key WHERE w.resolved_at IS NULL ORDER BY w.created_at, w.ticket_key',
            params,
        )

    def list_ticket_types_in_project(self, params: Any = ()) -> Any:
        """List key, type and parent_key for every ticket in a project."""
        return self._all(
            "list_ticket_types_in_project",
            'SELECT key,type,parent_key FROM tickets WHERE project=%s',
            params,
        )

    def list_orphaned_in_progress_keys(self, params: Any = ()) -> Any:
        """List In Progress tickets with no recent or running run."""
        return self._all(
            "list_orphaned_in_progress_keys",
            "SELECT t.key FROM tickets t WHERE t.status = 'In Progress' AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = t.key AND r.status = 'running') AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = t.key AND r.started_at > now() - interval '10 minutes') AND t.synced_at > now() - interval '5 minutes'",
            params,
        )

    def mark_ticket_todo(self, params: Any = ()) -> Any:
        """Set a ticket status back to To Do."""
        return self._run(
            "mark_ticket_todo",
            "UPDATE tickets SET status='To Do' WHERE key=%s",
            params,
        )

    def list_done_keys_among(self, params: Any = ()) -> Any:
        """Return which of the given keys are currently Done."""
        return self._all(
            "list_done_keys_among",
            "SELECT key FROM tickets WHERE key=ANY(%s) AND status='Done'",
            params,
        )

    def get_full_ticket_row(self, params: Any = ()) -> Any:
        """Return every column for one ticket."""
        return self._one(
            "get_full_ticket_row",
            'SELECT * FROM tickets WHERE key=%s',
            params,
        )


class FakeTicketRepository(TicketRepository):
    """In-memory ticket repository for tests, with canned results for named read queries."""

    def __init__(self, tickets: list[dict] | None = None, responses: dict[str, Any] | None = None) -> None:
        self._tickets = {str(row["key"]): deepcopy(row) for row in tickets or []}
        self._responses = deepcopy(responses or {})
        self.calls: list[tuple[str, Any]] = []

    def _one(self, name: str, sql: str, params: Any = ()) -> dict | None:
        self.calls.append((name, deepcopy(params)))
        if name == "get":
            return deepcopy(self._tickets.get(str(params[0])))
        return deepcopy(self._responses.get(name))

    def _all(self, name: str, sql: str, params: Any = ()) -> list[dict]:
        self.calls.append((name, deepcopy(params)))
        return deepcopy(self._responses.get(name, []))

    def _run(self, name: str, sql: str, params: Any = ()) -> int:
        self.calls.append((name, deepcopy(params)))
        if name == "update_status":
            status, key = params
            if str(key) in self._tickets:
                self._tickets[str(key)]["status"] = status
        elif name == "remove_label":
            label, key = params
            if str(key) in self._tickets:
                self._tickets[str(key)]["labels"] = _labels_without(
                    self._tickets[str(key)].get("labels", []), label)
        elif name == "update_description":
            description, key = params
            if str(key) in self._tickets:
                self._tickets[str(key)]["description"] = description
        elif name == "scale_mark_done":
            key = str(params[0])
            if key in self._tickets:
                self._tickets[key]["status"] = "Done"
        elif name == "scale_restore_todo":
            key = str(params[0])
            if key in self._tickets and self._tickets[key]["status"] == "Done":
                self._tickets[key]["status"] = "To Do"
        return int(self._responses.get(name, 1))

    def _execute(self, name: str, sql: str, params: Any = ()) -> Any:
        self.calls.append((name, deepcopy(params)))
        return deepcopy(self._responses.get(name))

    def cleanup_hot_file_reservations(self, connection: Any, grace_s: int) -> None:
        self.calls.append(("cleanup_hot_file_reservations", grace_s))

    def scale_history_tickets(self, cursor: Any, rows: list[tuple]) -> None:
        self.calls.append(("scale_history_tickets", deepcopy(rows)))
        for key, project, status, labels, summary, description, *_ in rows:
            self._tickets[str(key)] = {"key": key, "project": project, "status": status, "type": "Task",
                                       "priority": "Medium", "summary": summary, "description": description,
                                       "labels": deepcopy(labels), "parent_key": None, "blocked_by": []}

    def scale_wave_tickets(self, cursor: Any, rows: list[tuple]) -> None:
        self.calls.append(("scale_wave_tickets", deepcopy(rows)))
        for key, project, labels, summary, description, priority, blocked_by, *_ in rows:
            self._tickets[str(key)] = {"key": key, "project": project, "status": "To Do", "type": "Task",
                                       "priority": priority, "summary": summary, "description": description,
                                       "labels": deepcopy(labels), "parent_key": None,
                                       "blocked_by": deepcopy(blocked_by)}
