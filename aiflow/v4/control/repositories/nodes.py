"""Machines (`nodes`): the queries the inventory service needs, for a pooled DB or an open transaction."""
from __future__ import annotations

from ..db import J


def _all(queryable, sql: str, params: tuple = ()) -> list[dict]:
    if hasattr(queryable, "all"):
        return queryable.all(sql, params)
    return list(queryable.execute(sql, params).fetchall())


def removed_names(queryable) -> set[str]:
    return {row["name"] for row in _all(queryable, "SELECT name FROM nodes WHERE removed_at IS NOT NULL")}


def managed_assignments(queryable) -> dict[str, list[str]]:
    """Projects created through the console, per machine (kept apart from the file-synced `projects`)."""
    return {row["name"]: list(row["managed_projects"] or []) for row in _all(
        queryable, "SELECT name,managed_projects FROM nodes WHERE removed_at IS NULL AND managed_projects<>'{}'")}


def active(queryable) -> list[dict]:
    return _all(queryable, "SELECT name,ip,role,slots_total,projects,profile FROM nodes WHERE removed_at IS NULL")


def upsert_imported(conn, name: str, ip: str, role: str, slots: int, projects: list[str], profile: dict) -> None:
    """Insert a machine from a node file; a row synced earlier keeps its live heartbeat state."""
    conn.execute(
        "INSERT INTO nodes (name, ip, role, slots_total, projects, dynamic, profile)"
        " VALUES (%s,%s,%s,%s,%s,false,%s) ON CONFLICT (name) DO UPDATE SET dynamic=false,"
        " projects=nodes.projects || ARRAY(SELECT imported.project FROM unnest(EXCLUDED.projects)"
        " AS imported(project) WHERE NOT (imported.project = ANY(nodes.projects))),"
        " profile=EXCLUDED.profile",
        (name, ip, role, slots, projects, J(profile)))


def upsert_assignment_only(conn, name: str, projects: list[str]) -> None:
    """A project assigned a machine that has no node file: keep the assignment in the registry."""
    conn.execute(
        "INSERT INTO nodes (name, projects, dynamic, status, profile) VALUES (%s,%s,true,'offline','{}')"
        " ON CONFLICT (name) DO UPDATE SET dynamic=true, projects=nodes.projects || ARRAY("
        " SELECT imported.project FROM unnest(EXCLUDED.projects) AS imported(project)"
        " WHERE NOT (imported.project = ANY(nodes.projects)))",
        (name, projects))


def assign_project(conn, key: str, nodes: list[str]) -> None:
    """Let the given machines serve a project (both the live and the console-managed lists)."""
    for column in ("projects", "managed_projects"):
        conn.execute(
            f"UPDATE nodes SET {column}=CASE WHEN %s=ANY({column}) THEN {column}"
            f" ELSE array_append({column},%s) END WHERE name=ANY(%s) AND removed_at IS NULL",
            (key, key, nodes))


def tools_and_metrics(db, node: str) -> dict:
    return db.one("SELECT tools,metrics FROM nodes WHERE name=%s", (node,)) or {}
