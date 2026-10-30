"""Reactions to a new incident: the telemetry row, the study case and the alert (they were one function)."""
from __future__ import annotations

import logging

from ...platform.events import IN_TRANSACTION, Registry
from .events import IncidentOpened

log = logging.getLogger("aiflow4.incidents")


def register_listeners(ctx, registry: Registry) -> None:
    @registry.on(IncidentOpened, delivery=IN_TRANSACTION, name="incidents.telemetry_incident_opened")
    def telemetry(envelope, item: IncidentOpened) -> None:
        ctx.events.emit("incident.opened", reason=item.title, outcome=item.severity, kind=item.kind,
                        subject=item.subject, detail=item.detail)

    @registry.on(IncidentOpened, name="incidents.write_study_case")
    def study_case(envelope, item: IncidentOpened) -> None:
        """Keep a study case for the incident; a failure here must not stop the alert, so it is only logged."""
        from ... import knowledge
        ticket = ctx.db.one("SELECT key,project,parent_key FROM tickets WHERE key=%s", (item.subject,))
        latest_run = (ctx.db.one("SELECT id FROM runs WHERE ticket_key=%s ORDER BY started_at DESC LIMIT 1",
                                 (item.subject,)) if ticket else None)
        try:
            knowledge.write_study_case(
                ctx.db, project=ticket["project"] if ticket else "", scope="project" if ticket else "general",
                epic=knowledge._ticket_epic(ctx.db, item.subject, ticket.get("parent_key") or "") if ticket else "",
                title=f"Incident: {item.title[:170]}", situation=item.detail or item.title, cause=item.title,
                fix="Resolution pending while this incident is open.",
                prevention="Use the incident trigger and recovery checks when handling a similar condition.",
                source_type="incident", source_ref=str(item.incident_id),
                source_url=(f"{ctx.settings.jira_site.rstrip('/')}/browse/{item.subject}"
                            if ticket and ctx.settings.jira_site else ""),
                ticket_key=item.subject if ticket else "", run_id=str(latest_run["id"]) if latest_run else "")
        except Exception as exc:  # noqa: BLE001 - incident alerts still flow if knowledge storage is unavailable
            log.warning("incident knowledge capture failed: %s", type(exc).__name__)

    @registry.on(IncidentOpened, name="incidents.alert")
    def alert(envelope, item: IncidentOpened) -> None:
        """Tell people about a critical or warning incident once, and remember whether a channel took it."""
        if item.severity not in ("critical", "warning"):
            return
        from ... import incidents, notify
        channels = notify.send(item.severity, item.title, item.detail, ctx.settings.public_url + "/cc", ctx=ctx,
                               alert_kind="critical_incident" if item.severity == "critical" else "ticket_needs_person",
                               ticket=item.subject if incidents._TICKET_KEY.fullmatch(item.subject or "") else "",
                               needed=item.detail or item.title)
        ctx.db.run("UPDATE incidents SET notified=%s WHERE id=%s", (bool(channels), item.incident_id))
