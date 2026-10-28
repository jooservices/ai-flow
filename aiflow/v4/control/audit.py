"""The audit log (docs/v4/security.md): who changed settings, people, roles or credentials, and when.

Rows are only ever inserted. The table `audit_log` has triggers that refuse UPDATE, DELETE and TRUNCATE, and the console
exposes one read route (`GET /v1/security/audit`) and nothing that writes, so an administrator cannot edit or delete the
trail from the console. Details never contain secrets: they name what changed, not the value.
"""
from __future__ import annotations

from .db import J

CATEGORIES = ("auth", "people", "roles", "credentials", "settings", "security")


def record(db, actor: str, category: str, action: str, target: str = "", detail: dict | None = None, ip: str = "") -> None:
    """Append one entry; callers include it in the same transaction as the change."""
    db.run("INSERT INTO audit_log (actor, category, action, target, ip, detail) VALUES (%s,%s,%s,%s,%s,%s)",
           (actor[:200], category, action[:200], target[:300], ip[:64], J(detail or {})))


def record_console_user_change(request, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
    """Append an account or role change using the request's trusted client address."""
    from .login import client_ip

    ctx = request.app.state.ctx
    category = "roles" if action == "roles_changed" else "people"
    record(ctx.db, actor, category, action, target, detail, client_ip(request, ctx.settings.trusted_proxies))


def record_console_user_roles_change(request, actor: str, target: str, before, after, project_keys) -> None:
    before, after = list(before), list(after)
    action = "roles_changed" if before != after else "person_updated"
    record_console_user_change(request, actor, action, target,
                               {"roles_before": before, "roles": after, "project_keys": project_keys})


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
