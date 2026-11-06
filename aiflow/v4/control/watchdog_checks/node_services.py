"""Watchdog check: an unhealthy test service keeps a node off that project's work."""
from __future__ import annotations

from ...core.services import service_gaps
from ..application_context import ApplicationContext
from ..incidents import close_incident, open_incident
from ..repositories.watchdog import ONLINE_NODES_FOR_SERVICES, OPEN_NODE_SERVICE_INCIDENTS


def node_services(ctx: ApplicationContext) -> None:
    repo = ctx.repo()
    # declared test services (PostgreSQL, Redis): an unhealthy one keeps the node from taking that project's work
    service_issues: set[str] = set()
    for n in ctx.db.all(ONLINE_NODES_FOR_SERVICES):
        for key in repo.projects_for_node(n["name"], n["projects"] or [], n["dynamic"]):
            project = repo.projects.get(key)
            gaps = service_gaps((n["metrics"] or {}).get("services"), project.required_services) if project else {}
            if not gaps:
                continue
            subject = f"{n['name']}:{key}"
            service_issues.add(subject)
            open_incident(ctx, "node_services", subject, "warning", f"node {n['name']} has unhealthy test services for {key}",
                          f"{n['name']} takes no {key} work until its test services are healthy: "
                          + "; ".join(f"{svc} ({why})" for svc, why in gaps.items())
                          + ". The node agent recreates missing or stopped services; the incident closes when a later"
                          " heartbeat reports them healthy.")
    for i in ctx.db.all(OPEN_NODE_SERVICE_INCIDENTS):
        if i["subject"] not in service_issues:
            close_incident(ctx, "node_services", i["subject"], "test services are healthy or the node no longer serves the project")
