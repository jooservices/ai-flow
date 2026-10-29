"""Tell the Jira assignee, or the configured owner, that a required human review starts (moved out of api.py)."""
from __future__ import annotations

from typing import Any

from . import notify
from .db import J


def notify_human_review(ctx: Any, key: str, ticket: dict | None = None) -> list[str]:
    """Notify the Jira assignee, or the configured owner, when a required human review starts."""
    ticket = ticket or ctx.db.one("SELECT key, project, labels, summary, fields FROM tickets WHERE key=%s", (key,))
    if not ticket or "require-human" not in (ticket.get("labels") or []):
        return []
    fields = ticket.get("fields") or {}
    assignee = ticket.get("assignee") or fields.get("assignee") or {}
    email = assignee.get("email") if isinstance(assignee, dict) else ""
    title = f"{key} is ready for human review"
    detail = ticket.get("summary") or key
    link = ctx.settings.public_url.rstrip("/") + "/review"
    sent = notify.send("info", title, detail, link, recipient=email or "", ctx=ctx,
                       alert_kind="review_before_merge", project=ticket.get("project", ""), ticket=key,
                       needed="Review the ticket before merge.")
    ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
               " VALUES ('notify','human-review',%s,'control',true,%s,%s)",
               (key, ",".join(sent) or "no channel configured", J({"assignee": bool(email)})))
    ctx.events.emit("notify.human_review", ticket=key, project=ticket.get("project", ""), account="control",
                    outcome="sent" if sent else "no_channel", reason="require-human")
    return sent
