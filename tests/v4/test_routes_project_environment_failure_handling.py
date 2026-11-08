"""A failed control repository commit answers 502 and is audited, as the environment save route has always done."""
from __future__ import annotations

from test_project_audit_events import clear_github_connection_context, owner_sign_in, prepare_project_control  # noqa: F401 - fixtures

ENVIRONMENT_ITEM = {"name": "APP_BASE_URL", "kind": "base_url", "value": "https://app.example.test"}


def test_a_failed_control_repository_commit_answers_502_and_audits_the_refusal(client, ctx, control_repo, monkeypatch):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)

    def refuse_commit(*_args, **_kwargs):
        raise RuntimeError("422: Update is not a fast forward")

    monkeypatch.setattr(ctx.github, "commit_file", refuse_commit)
    response = client.put("/v1/projects/TESTWP/environment", json={"items": [ENVIRONMENT_ITEM]})

    assert response.status_code == 502
    assert response.json()["detail"] == "could not save the environment to aiflow-control"
    audit = ctx.db.one("SELECT accepted, result FROM write_audit "
                       "WHERE action='save_project_environment' ORDER BY id DESC")
    assert audit == {"accepted": False, "result": "control repo commit failed"}
