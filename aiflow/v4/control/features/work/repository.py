"""Queries the work feature runs against the registry: runs, the account being assigned, the node's tools and the board."""
from __future__ import annotations

from typing import Any

from ...db import DB, J
from .models import AccountForecast, BoardTicket, NodeTools, RunNote


class WorkRepository:
    def __init__(self, db: DB) -> None:
        self.db = db

    def account_forecast(self, account_id: str) -> AccountForecast | None:
        row = self.db.one("SELECT id,quota_forecast FROM accounts WHERE id=%s", (account_id,))
        return AccountForecast(id=str(row["id"]), quota_forecast=row.get("quota_forecast") or {}) if row else None

    def set_run_details(self, run_id: str, details: dict) -> None:
        self.db.run("UPDATE runs SET details=%s WHERE id=%s", (J(details), run_id))

    def inherit_pull_request(self, run_id: str, prior_dev: dict) -> None:
        """A rework run carries the PR identity the previous developer run recorded, so the CI-log endpoint can bind
        branch and SHA requests to the ticket's pull request and role health does not blame a later run for it."""
        self.db.run("UPDATE runs SET pr_url=%s, branch=%s, head_sha=%s, details=details || %s WHERE id=%s",
                    (prior_dev["pr_url"], prior_dev["branch"], prior_dev.get("head_sha") or "",
                     J({"pr_url_source": "inherited"}), run_id))

    def mark_account_working(self, account_id: str) -> None:
        self.db.run("UPDATE accounts SET status='working', updated_at=now(), idle_since=NULL, cooldown_since=NULL"
                    " WHERE id=%s", (account_id,))

    def node_tools(self, node: str) -> NodeTools:
        row = self.db.one("SELECT tools,metrics FROM nodes WHERE name=%s", (node,)) or {}
        return NodeTools(tools=row.get("tools") or {}, metrics=row.get("metrics") or {})

    def latest_rework(self, ticket: str) -> RunNote | None:
        return _note(self.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s"
                           " AND step IN ('verify','qa') AND outcome='rework' ORDER BY started_at DESC LIMIT 1", (ticket,)))

    def latest_answered_triage(self, ticket: str) -> RunNote | None:
        return _note(self.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s AND step='triage'"
                           " AND outcome='answered' ORDER BY started_at DESC LIMIT 1", (ticket,)))

    def dev_finished_since(self, ticket: str, since: Any) -> bool:
        return bool(self.db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND step='dev' AND started_at > %s"
                                " AND outcome IN ('done', 'blocked') LIMIT 1", (ticket, since)))

    def blocked_question_before(self, ticket: str, before: Any) -> str:
        row = self.db.one("SELECT summary FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='blocked'"
                          " AND started_at < %s ORDER BY started_at DESC LIMIT 1", (ticket, before))
        return (row or {}).get("summary") or ""

    def open_project_tickets(self, project: str, exclude: str) -> list[BoardTicket]:
        rows = self.db.all("SELECT key, type, status, summary, parent_key FROM tickets WHERE project=%s AND status<>'Done'"
                           " AND key<>%s ORDER BY type<>'Epic', key LIMIT 300", (project, exclude))
        return [BoardTicket(key=r["key"], type=r["type"], status=r["status"], summary=r["summary"],
                            parent_key=r["parent_key"]) for r in rows]


def _note(row: dict | None) -> RunNote | None:
    return RunNote(summary=row["summary"], details=row["details"] or {}, started_at=row["started_at"]) if row else None
