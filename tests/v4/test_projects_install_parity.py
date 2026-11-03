"""Golden behavior cases for pure helpers moved out of the project route installer."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control.features.projects import routes


FIXTURE = Path(__file__).parent / "fixtures" / "projects-install-parity.jsonl"
CASES = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


class RecordingApp:
    """Provide the decorator interface needed to install routes without starting services."""

    def __getattr__(self, method):
        def factory(_path="", *_args, **_options):
            return lambda handler: handler

        return factory


def _route_helpers():
    settings = SimpleNamespace(github_app_dir="", github_org="", public_url="")
    ctx = SimpleNamespace(settings=settings)
    deps = SimpleNamespace(audit=lambda *_args, **_kwargs: None,
                           console_user=lambda *_args, **_kwargs: None,
                           fenced=lambda *_args, **_kwargs: None,
                           node_auth=lambda *_args, **_kwargs: None,
                           optional_console_user=lambda *_args, **_kwargs: None,
                           require_capacity=lambda *_args, **_kwargs: None)
    return routes.install(RecordingApp(), ctx, deps)


@pytest.mark.parametrize("case", CASES, ids=[case["helper"] for case in CASES])
def test_project_route_helpers_match_golden_cases(case):
    args = case.get("args", [])
    if case["helper"] == "_project_environment_content":
        items = [SimpleNamespace(**item) for item in case["items"]]
        actual = getattr(routes, case["helper"])(case["config"], items)
    elif case["helper"] == "_project_preflight_failure":
        body = SimpleNamespace(**case["body"])
        actual = getattr(routes, case["helper"])(body, case["result"])
    elif case["helper"] == "_new_project_config":
        body = SimpleNamespace(**case["body"])
        actual = routes._new_project_config(body, case["key"])
    elif case["helper"].startswith("_"):
        if case["helper"] == "_decode_github_import":
            args = [args[0].encode()]
        actual = getattr(routes, case["helper"])(*args)
        if isinstance(actual, tuple):
            actual = list(actual)
    elif case["helper"] == "repository_settings_payload":
        helper = getattr(_route_helpers(), case["helper"])
        project_data = case["project"]
        project = SimpleNamespace(**project_data,
                                  settings_for_repo=lambda name: {"repo": name})
        actual = helper(project)
    else:
        helper = getattr(_route_helpers(), case["helper"])
        try:
            actual = helper(*args)
        except HTTPException as exc:
            actual = {"error": {"status": exc.status_code, "detail": exc.detail}}
    expected = case.get("expected", {"error": case.get("error")})
    assert actual == expected
