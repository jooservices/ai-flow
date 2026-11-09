"""Read the project listing for the Projects screen."""
from ....core import pipeline_checks
from ... import system_check_api, tooling
from . import repository


def managed_project_list(ctx, repo) -> list[dict]:
    """Build the existing project rows from one batched registry read and the live repository snapshot."""
    data = repository.listing_data(ctx.db)
    readiness_by_project = system_check_api.projects_readiness(ctx, repo, data.node_rows)
    probes = tooling.probe_report(ctx, repo=repo, probe_rows=data.probe_rows)
    projects = []
    for project in sorted(repo.projects.values(), key=lambda item: item.key):
        local_checks, pipeline_only = pipeline_checks.project_catalog(project.local_gate)
        projects.append({"key": project.key, "repo": project.repo,
                         "repositories": project.configured_repositories,
                         "status": "paused" if project.paused else "active",
                         "priority": project.priority, "ticket_count": data.ticket_counts.get(project.key, 0),
                         "base": project.base, "stack": project.stack,
                         "node_readiness": readiness_by_project.get(project.key, []),
                         "services": project.required_services,
                         "checks": probes.get(project.key, {}), "check_commands": list(project.checks),
                         "pipeline_checks": local_checks, "pipeline_only": pipeline_only})
    return projects
