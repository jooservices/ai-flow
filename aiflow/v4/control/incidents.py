"""Incidents: opened once per (kind, subject) while the condition holds, closed when it clears; people are
alerted on open (critical and warning) and on resolve. Used by the watchdog and by the API (heartbeats)."""
from __future__ import annotations

from . import notify


def open_incident(ctx, kind: str, subject: str, severity: str, title: str, detail: str = "") -> bool:
    """True when a new incident was opened (so callers alert once, not every pass)."""
    row = ctx.db.one("INSERT INTO incidents (kind, subject, severity, title, detail) VALUES (%s,%s,%s,%s,%s)"
                     " ON CONFLICT (kind, subject) WHERE closed_at IS NULL DO NOTHING RETURNING id",
                     (kind, subject, severity, title, detail))
    if row:
        ctx.events.emit("incident.opened", reason=title, outcome=severity, kind=kind, subject=subject, detail=detail)
        if severity in ("critical", "warning"):
            channels = notify.send(severity, title, detail, ctx.settings.public_url + "/cc")
            ctx.db.run("UPDATE incidents SET notified=%s WHERE id=%s", (bool(channels), row["id"]))
    return bool(row)


def close_incident(ctx, kind: str, subject: str, note: str = "") -> None:
    row = ctx.db.one("UPDATE incidents SET closed_at=now() WHERE kind=%s AND subject=%s AND closed_at IS NULL RETURNING title",
                     (kind, subject))
    if row:
        ctx.events.emit("incident.closed", reason=note or row["title"], kind=kind, subject=subject)
        notify.send("info", f"resolved: {row['title']}", note)
