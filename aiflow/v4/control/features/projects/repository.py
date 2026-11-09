"""Database reads shared by the Projects screen listing."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProjectListingData:
    ticket_counts: dict[str, int]
    node_rows: list[dict[str, Any]]
    probe_rows: list[dict[str, Any]]


def listing_data(db) -> ProjectListingData:
    """Read project counts, node readiness and check probes in one database round trip."""
    row = db.one(
        "SELECT (SELECT coalesce(jsonb_object_agg(project, ticket_count), '{}'::jsonb) FROM "
        "(SELECT project, count(*) AS ticket_count FROM tickets GROUP BY project) counts) AS ticket_counts, "
        "(SELECT coalesce(jsonb_agg(jsonb_build_object('name', name, 'readiness', readiness, "
        "'projects', projects, 'dynamic', dynamic) ORDER BY name), '[]'::jsonb) FROM nodes) AS node_rows, "
        "(SELECT coalesce(jsonb_agg(jsonb_build_object('project', project, 'node', node, "
        "'signature', signature, 'checked_at', checked_at::text, 'results', results) ORDER BY node), "
        "'[]'::jsonb) FROM project_check_probes) AS probe_rows")
    return ProjectListingData(ticket_counts=row["ticket_counts"], node_rows=row["node_rows"],
                              probe_rows=row["probe_rows"])


def report_by_lifecycle_key(connection, lifecycle_key: str) -> dict[str, Any] | None:
    """Find a previously saved report for an idempotent lifecycle follow-up."""
    return connection.execute("SELECT id,version,report,markdown,created_at FROM project_reports"
                              " WHERE lifecycle_key=%s", (lifecycle_key,)).fetchone()


def insert_report(connection, project: str, scope: str, subject: str, version: int, report: Any,
                  markdown: str, lifecycle_key: str | None) -> dict[str, Any]:
    """Insert one report snapshot and return its persisted identity."""
    return connection.execute("INSERT INTO project_reports"
                              " (project,scope,subject,version,report,markdown,lifecycle_key)"
                              " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id,created_at",
                              (project, scope, subject, version, report, markdown, lifecycle_key)).fetchone()
