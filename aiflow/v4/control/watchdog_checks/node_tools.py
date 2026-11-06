"""Watchdog check: an online node must have the tools its projects need."""
from __future__ import annotations

from ...core.toolchains import toolchain_gaps
from ..application_context import ApplicationContext
from ..incidents import close_incident, open_incident
from ..repositories.watchdog import NODES_TOOLS, OPEN_NODE_TOOL_INCIDENTS


def node_tools(ctx: ApplicationContext) -> None:
    repo = ctx.repo()
    # project stacks: an online node must have every tool its projects need, or a person installs it
    repo = ctx.repo()
    tool_issues: set[str] = set()
    for n in ctx.db.all(NODES_TOOLS):
        for key in repo.projects_for_node(n["name"], n["projects"] or [], n["dynamic"]):
            project = repo.projects.get(key)
            if not project:
                continue
            subject = f"{n['name']}:{key}"
            if n["status"] != "online":
                # an offline node sends no heartbeat, so keep its incident: nobody confirmed the tools either way
                tool_issues.add(subject)
                continue
            gaps = toolchain_gaps(n["tools"], project.tools, repo.versions, project.stacks,
                                  require_pins=bool(project.stacks))
            if not gaps:
                continue
            tool_issues.add(subject)
            open_incident(ctx, "node_tools", subject, "warning", f"node {n['name']} misses tools for {key}",
                          f"Preflight on {n['name']} for {key} ({', '.join(project.stacks) or 'no stack'}) is unhealthy: "
                          f"{', '.join(gaps)}. The node agent installs stack tools and retries after a release or "
                          "toolchain update; the incident closes when a later heartbeat passes preflight.")
        # the CLIs of the AI providers whose accounts run on the node
        subject = f"{n['name']}:ai"
        ai_missing = toolchain_gaps(n["tools"], repo.provider_tools_for_node(n["name"]), repo.versions) \
            if n["status"] == "online" else []
        if n["status"] != "online" and repo.provider_tools_for_node(n["name"]):
            tool_issues.add(subject)
        elif ai_missing:
            tool_issues.add(subject)
            open_incident(ctx, "node_tools", subject, "warning", f"node {n['name']} misses AI tools",
                          f"Missing on {n['name']} for its accounts: {', '.join(ai_missing)}. Install them on the node; the"
                          " incident closes when a later heartbeat reports them.")
    for i in ctx.db.all(OPEN_NODE_TOOL_INCIDENTS):
        if i["subject"] not in tool_issues:
            close_incident(ctx, "node_tools", i["subject"], "tools are installed or the node no longer serves the project")
