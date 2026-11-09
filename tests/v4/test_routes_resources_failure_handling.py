"""Pin the 502 translation of a control-repo write failure in the knowledge resources routes."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control.features.knowledge import routes_resources
from aiflow.v4.control.github import GitHubError


def _fake_knowledge():
    class Invalid(Exception):
        pass

    def validate(kind, resource_id, title, content, actor):
        return kind, resource_id, title, content, actor

    return SimpleNamespace(
        Invalid=Invalid,
        validate=validate,
        import_local=lambda _db, _root: None,
        get_resource=lambda _db, _kind, _resource_id: None,
        path_for=lambda _kind, _resource_id: "docs/resource.md",
        file_content=lambda _kind, _resource_id, _title, _content, _archived: "payload",
    )


def test_a_control_repo_write_failure_is_still_a_502_and_saves_nothing(monkeypatch, caplog):
    class Github:
        def default_branch(self, _repo_name):
            return "main"

        def commit_file(self, _repo_name, _branch, _path, _payload, _message):
            raise GitHubError("PUT contents -> 403: branch protected", status_code=403)

    saved = []
    knowledge = _fake_knowledge()

    def save_current(*_args, **_kwargs):
        saved.append(True)
        return {"version": 1}

    knowledge.save_current = save_current
    monkeypatch.setattr(routes_resources, "knowledge", knowledge)
    monkeypatch.setattr(routes_resources, "github_control_gateway", lambda _ctx: Github())
    ctx = SimpleNamespace(db=object(), repo=lambda: SimpleNamespace(root="root"),
                          settings=SimpleNamespace(control_repo="acme/control"))

    with pytest.raises(HTTPException) as error:
        routes_resources._submit_resource_change(ctx, "guide", "onboarding", "Onboarding", "body", False,
                                                 "owner", "save")

    assert error.value.status_code == 502
    assert error.value.detail == "control repo could not save the resource"
    assert saved == []
    assert "control repo write failed for guide/onboarding" in caplog.text
