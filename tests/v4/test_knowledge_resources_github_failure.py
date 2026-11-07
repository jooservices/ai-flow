"""A GitHub failure while saving a knowledge resource answers 502 with a fixed message."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control.features.knowledge import routes_resources
from aiflow.v4.control.github import GitHubError


class FailingGithub:
    def default_branch(self, _repo_name):
        raise GitHubError("GET /repos/org/control -> 502: upstream unavailable", status_code=502)


def test_github_failure_while_saving_a_resource_answers_502_with_the_fixed_message(monkeypatch):
    monkeypatch.setattr(routes_resources, "_load_managed_resources", lambda _ctx: None)
    monkeypatch.setattr(routes_resources, "github_control_gateway", lambda _ctx: FailingGithub())
    ctx = SimpleNamespace(db=SimpleNamespace(one=lambda *_args: None),
                          settings=SimpleNamespace(control_repo="/srv/aiflow-control"))

    with pytest.raises(HTTPException) as error:
        routes_resources._submit_resource_change(ctx, "skill", "release-notes", "Release notes", "content", False,
                                                  "owner", "save")

    assert error.value.status_code == 502
    assert error.value.detail == "control repo could not save the resource"
