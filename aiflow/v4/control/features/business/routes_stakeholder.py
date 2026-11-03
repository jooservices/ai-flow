"""Stakeholder acceptance and UAT feedback."""
from types import SimpleNamespace

from . import stakeholder_steps
from .bodies import StakeholderVerdictBody


def install_stakeholder_routes(app, ctx, log) -> SimpleNamespace:
    def stakeholder_project(key: str):
        return stakeholder_steps.configured_project(ctx, key)

    def stakeholder_changes(project: str) -> list[dict]:
        return stakeholder_steps.recent_changes(ctx, project)

    @app.get("/v1/stakeholder/{key}")
    def stakeholder_view(key: str) -> dict:
        project = stakeholder_project(key)
        configured = project.urls or {}
        return {"project": key, "urls": {"develop": str(configured.get("develop") or ""),
                                          "production": str(configured.get("production") or "")},
                "changes": stakeholder_changes(key)}

    @app.post("/v1/stakeholder/{key}/verdict")
    def stakeholder_verdict(key: str, body: StakeholderVerdictBody) -> dict:
        project = stakeholder_project(key)
        change, target, sender, screenshot = stakeholder_steps.check_verdict(ctx, key, body)

        page_url = str((project.urls or {}).get(body.environment) or "").strip()
        page_label = page_url or "chưa deploy"
        release_commit = body.release_commit.strip() or "chưa có dữ liệu"
        if body.verdict == "expected":
            return stakeholder_steps.record_acceptance(ctx, log, key, body, target, sender, release_commit)
        return stakeholder_steps.file_uat_feedback(ctx, log, key, body, change, sender, screenshot, page_label,
                                                   release_commit)

    return SimpleNamespace(stakeholder_project=stakeholder_project, stakeholder_changes=stakeholder_changes,
                           stakeholder_view=stakeholder_view, stakeholder_verdict=stakeholder_verdict)
