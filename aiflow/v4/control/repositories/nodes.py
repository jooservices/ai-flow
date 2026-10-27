"""Persistence queries for node state used by control services."""
from __future__ import annotations


class NodesRepository:
    def __init__(self, db) -> None:
        self.db = db

    def candidate_node(self, node: str) -> dict:
        """Node data needed to decide which work an account can receive there."""
        return self.db.one("SELECT name,status,last_heartbeat,projects,dynamic,tools,metrics,accepting_work,"
                           "release_version FROM nodes WHERE name=%s AND removed_at IS NULL", (node,)) or {}

    def dashboard_nodes(self) -> list[dict]:
        """Node rows used to assemble the Command Center registry snapshot."""
        return self.db.all("SELECT * FROM nodes WHERE removed_at IS NULL ORDER BY role DESC, name")

    def removed_names(self) -> set[str]:
        """Removed node names excluded from the live control-repo view."""
        return {row["name"] for row in self.db.all("SELECT name FROM nodes WHERE removed_at IS NOT NULL")}

    def configured_node_removed(self, name: str) -> bool | None:
        row = self.db.one("SELECT removed_at FROM nodes WHERE name=%s", (name,))
        return None if row is None else bool(row["removed_at"])

    def sync_configured(self, node) -> None:
        self.db.run("INSERT INTO nodes (name,ip,role,slots_total,projects) VALUES (%s,%s,%s,%s,%s)"
                    " ON CONFLICT (name) DO UPDATE SET role=EXCLUDED.role,slots_total=EXCLUDED.slots_total,"
                    " projects=EXCLUDED.projects,dynamic=false",
                    (node.name, node.ip, node.role, node.slots, node.projects))

    def claim_tooling(self, name: str) -> dict:
        return self.db.one("SELECT tools,metrics FROM nodes WHERE name=%s", (name,)) or {}
