"""Error handling for the project catalog route when upstream providers fail."""
from __future__ import annotations

from test_projects import owner_sign_in


def test_project_catalog_reports_bad_gateway_when_upstream_provider_fails(client, ctx, monkeypatch) -> None:
    """Return HTTP 502 when loading the connected Jira projects or GitHub repositories fails."""
    owner_sign_in(client, ctx)

    def failing_projects() -> list[dict[str, str]]:
        raise RuntimeError("simulated upstream Jira outage")

    monkeypatch.setattr(ctx.jira, "projects", failing_projects)

    response = client.get("/v1/projects/catalog")

    assert response.status_code == 502
    assert response.json()["detail"] == "could not load connected Jira projects and GitHub repositories"
