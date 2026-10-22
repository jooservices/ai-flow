"""The audit log (docs/v4/security.md): who changed settings, people, roles or credentials, and when.

Rows are only ever inserted. The table `audit_log` has triggers that refuse UPDATE, DELETE and TRUNCATE, and the console
exposes one read route (`GET /v1/security/audit`) and nothing that writes, so an administrator cannot edit or delete the
trail from the console. Details never contain secrets: they name what changed, not the value.
"""
from __future__ import annotations

import logging

from .db import J

log = logging.getLogger(__name__)

CATEGORIES = ("auth", "people", "roles", "credentials", "settings", "security")


def record(db, actor: str, category: str, action: str, target: str = "", detail: dict | None = None, ip: str = "") -> None:
    """Append one entry. A failure to write is logged loudly but never hides the change that was made."""
    try:
        db.run("INSERT INTO audit_log (actor, category, action, target, ip, detail) VALUES (%s,%s,%s,%s,%s,%s)",
               (actor[:200], category, action[:200], target[:300], ip[:64], J(detail or {})))
    except Exception:  # noqa: BLE001
        log.exception("audit log: could not record %s %s by %s", category, action, actor)


def category_for(method: str, path: str) -> str:
    """Which part of the product a console change belongs to, from the route it used."""
    if path.startswith("/v1/integrations"):
        return "credentials"
    if path.startswith("/v1/security"):
        return "security"
    return "settings"


def listing(db, category: str = "", actor: str = "", before: int = 0, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    clauses, params = [], []
    if category:
        clauses.append("category=%s")
        params.append(category)
    if actor:
        clauses.append("actor=%s")
        params.append(actor)
    if before:
        clauses.append("id<%s")
        params.append(int(before))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.all(f"SELECT id, ts, actor, category, action, target, ip, detail FROM audit_log{where}"
                  " ORDER BY id DESC LIMIT %s", (*params, limit))
    for r in rows:
        r["ts"] = r["ts"].isoformat()
    return rows
