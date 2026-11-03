"""Characterization of the project repository save route before extraction."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI, HTTPException

from conftest import FakeGitHub, FakeJira
from aiflow.v4.control.features.projects import routes as project_routes
from aiflow.v4.control.features.projects.bodies import ProjectRepositoriesBody


ACTOR = "owner@example.test"
FIXTURE = Path(__file__).parent / "fixtures" / "project_repositories_parity.jsonl"
CASES = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


def _raise(*_args, **_kwargs):
    raise RuntimeError("provider unavailable")


def _configure_provider_case(ctx, setup: str) -> None:
    if setup == "no_jira":
        ctx.jira = None
    elif setup == "no_github":
        ctx.github = None
    elif setup == "jira_error":
        ctx.jira.search = _raise
    elif setup == "github_error":
        ctx.github.installation_repositories = _raise


def _configure_epic_case(ctx, setup: str) -> None:
    if setup in {"known_epic", "known_epic_feature_branch"}:
        ctx.jira.search_data = [{"key": "TESTWP-12", "fields": {"issuetype": {"name": "Epic"}}}]
        if setup == "known_epic_feature_branch":
            ctx.github.available_branches[("new-app", "feature/abc")] = True


def _configure_assignment_case(ctx, setup: str) -> None:
    if setup == "stale_assignment":
        from aiflow.v4.control import gateway_helpers

        gateway_helpers.github_connections(ctx).assign("jooservices/new-app", "retired-connection")


def _configure_branch_case(ctx, setup: str) -> None:
    if setup == "default_branch_missing":
        ctx.github.available_branches[("test-aiflow-wp", "develop")] = False
    elif setup == "long_branch":
        ctx.github.available_branches[("test-aiflow-wp", "b" * 120)] = True
    elif setup == "one_character_branch":
        ctx.github.available_branches[("new-app", "x")] = True


def _configure_commit_case(ctx, setup: str) -> None:
    if setup == "commit_error":
        ctx.github.commit_file = _raise
    elif setup == "commit_http_error":
        ctx.github.commit_file = lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPException(409, "write conflict"))


def _make_context(case: dict, calls: list, root: Path):
    control_repo = root / "aiflow-control"
    (control_repo / "projects").mkdir(parents=True)
    (control_repo / "projects" / "TESTWP.yaml").write_text(
        "key: TESTWP\nrepo: jooservices/test-aiflow-wp\nbase: develop\nchecks: []\n", encoding="utf-8")
    github, jira = FakeGitHub(), FakeJira()
    project = SimpleNamespace(key="TESTWP", repo="jooservices/test-aiflow-wp", base="develop")
    settings = SimpleNamespace(control_repo=str(control_repo), github_app_dir=str(root / "github"),
                               github_org="jooservices")
    ctx = SimpleNamespace(settings=settings, github=github, jira=jira,
                          repo=lambda: SimpleNamespace(root=control_repo, projects={"TESTWP": project}),
                          refresh_repo=lambda: calls.append(["repo.refresh"]))
    ctx.events = SimpleNamespace(emit=lambda event_type, **_fields: calls.append(["events.emit", event_type]))
    setup = case["setup"]
    _configure_provider_case(ctx, setup)
    _configure_epic_case(ctx, setup)
    _configure_assignment_case(ctx, setup)
    _configure_branch_case(ctx, setup)
    _configure_commit_case(ctx, setup)
    return ctx


def _exercise(case: dict, monkeypatch) -> dict:
    root = Path(".tmp") / f"project-repositories-parity-{uuid.uuid4().hex}"
    calls: list[list] = []
    ctx = _make_context(case, calls, root)
    try:
        def audit(*args):
            calls.append(["audit", *args])

        def console_user(*_args, **_kwargs):
            if case["setup"] == "auth_refused":
                raise HTTPException(403, "projects.manage permission required")
            return SimpleNamespace(subject=ACTOR)

        deps = SimpleNamespace(audit=audit, console_user=console_user,
                               fenced=lambda *_args, **_kwargs: None, node_auth=lambda *_args, **_kwargs: None,
                               optional_console_user=lambda *_args, **_kwargs: None,
                               require_capacity=lambda *_args, **_kwargs: None)
        app = FastAPI()
        namespace = project_routes.install(app, ctx, deps)
        assert hasattr(namespace, "save_project_repositories")
        monkeypatch.setattr(project_routes, "publish_for",
                            lambda _ctx, event, **_kwargs: calls.append(["publish", type(event).__name__]))
        if ctx.jira is not None:
            original = ctx.jira.search
            ctx.jira.search = lambda *args, _original=original, **kwargs: (
                calls.append(["jira.search", *args]) or _original(*args, **kwargs))
        if ctx.github is not None:
            for name, label in (("installation_repositories", "github.repositories"),
                                ("branch_exists", "github.branch"), ("commit_file", "github.commit")):
                original = getattr(ctx.github, name)
                setattr(ctx.github, name, lambda *args, _original=original, _label=label, **kwargs:
                        (calls.append([_label, *args]) or _original(*args, **kwargs)))
        try:
            body = ProjectRepositoriesBody.model_validate(case["body"])
            result = namespace.save_project_repositories(case["key"], body, None)
            response = {"status": 200, "body": result}
        except HTTPException as exc:
            response = {"status": exc.status_code, "body": {"detail": exc.detail}}
        from aiflow.v4.control import gateway_helpers

        assignments = (gateway_helpers.github_connections(ctx).assignments() if ctx.github is not None else {})
        return {**response, "calls": calls, "assignments": assignments}
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_project_repository_save_matches_unchanged_behavior(case, monkeypatch):
    actual = _exercise(case, monkeypatch)
    assert actual == case["expected"]


def test_load_project_repository_save_context_calls_providers_in_order():
    calls = []
    gateway = object()
    ctx = object()
    available, gateways, epics = project_routes.load_project_repository_save_context(
        "TESTWP", ACTOR, ctx,
        lambda key: calls.append(["jira", key]) or [{"key": "TESTWP-12"}],
        lambda actor, source: calls.append(["catalog", actor, source]) or [
            {"full_name": "jooservices/new-app"}],
        lambda ctx: calls.append(["connections", ctx]) or [SimpleNamespace(id="default", gateway=gateway)])
    assert calls[:2] == [["jira", "TESTWP"], ["catalog", ACTOR, "project_repository_save"]]
    assert calls[2][0] == "connections" and calls[2][1] is ctx
    assert available == {"jooservices/new-app": {"full_name": "jooservices/new-app"}}
    assert gateways == {"default": gateway} and epics == {"TESTWP-12"}


def test_collect_requested_project_repositories_normalizes_and_refuses_bad_stack():
    available = {"jooservices/new-app": {"connections": ["default"]}}
    body = ProjectRepositoriesBody.model_validate({"repositories": [{
        "repo": " jooservices/new-app ", "base": " ", "checks": [" pytest ", " "],
        "stack": "python-3.12", "connection": "default"}]})
    repos, chosen, error = project_routes.collect_requested_project_repositories(body, available, "main")
    assert repos["jooservices/new-app"] == {"repo": "jooservices/new-app", "base": "", "checks": ["pytest"],
                                            "stack": "python-3.12"}
    assert chosen == {"jooservices/new-app": "default"} and error == ""
    bad = ProjectRepositoriesBody.model_validate({"repositories": [{"repo": "jooservices/new-app", "stack": "?"}]})
    assert project_routes.collect_requested_project_repositories(bad, available, "main")[2] == (
        "unsupported stack for jooservices/new-app")


def test_project_epic_repository_mappings_trim_and_drop_blank_targets():
    body = ProjectRepositoriesBody.model_validate({"epic_repositories": {" TESTWP-12 ": " jooservices/new-app ",
                                                                            "TESTWP-13": " "}})
    assert project_routes.project_epic_repository_mappings(body) == {"TESTWP-12": "jooservices/new-app"}


def test_add_epic_repository_mappings_validates_and_adds_visible_repositories():
    repos = {}
    available = {"jooservices/new-app": {}}
    assert project_routes.add_epic_repository_mappings("TESTWP", {"TESTWP-12": "jooservices/new-app"},
                                                       {"TESTWP-12"}, available, repos) == ""
    assert repos == {"jooservices/new-app": {"repo": "jooservices/new-app", "base": "", "checks": [],
                                              "stack": ""}}
    assert project_routes.add_epic_repository_mappings("TESTWP", {"OTHER-1": "jooservices/new-app"},
                                                       {"TESTWP-12"}, available, repos).startswith("OTHER-1 ")


def test_default_repository_is_added_only_when_missing():
    project = SimpleNamespace(repo="jooservices/default")
    repos = {}
    project_routes.include_default_project_repository(project, repos)
    assert repos["jooservices/default"] == {"repo": "jooservices/default", "base": "", "checks": [], "stack": ""}
    project_routes.include_default_project_repository(project, repos)
    assert len(repos) == 1


def test_first_project_repository_branch_error_uses_requested_connection():
    gateway = SimpleNamespace(branch_exists=lambda repo, branch: repo == "new-app" and branch == "main")
    project = SimpleNamespace(base="develop")
    repos = {"jooservices/new-app": {"repo": "jooservices/new-app", "base": "main"}}
    assert project_routes.first_project_repository_branch_error(
        project, repos, {"jooservices/new-app": "chosen"}, {}, {"chosen": gateway}) == ""
    assert project_routes.first_project_repository_branch_error(project, repos, {}, {}, {}) == (
        "no GitHub connection can reach jooservices/new-app")


def test_validate_project_repository_save_adds_default_and_checks_branch():
    body = ProjectRepositoriesBody.model_validate({})
    available = {"jooservices/default": {"connection": "default"}}
    gateway = SimpleNamespace(branch_exists=lambda _repo, _branch: True)
    repos, chosen, mappings, error = project_routes.validate_project_repository_save(
        body, "TESTWP", SimpleNamespace(repo="jooservices/default", base="main"), set(), available,
        {"default": gateway})
    assert repos["jooservices/default"]["repo"] == "jooservices/default"
    assert chosen == {} and mappings == {} and error == ""


def test_commit_project_repository_settings_sorts_and_commits_control_yaml():
    root = Path(".tmp") / f"project-repositories-helper-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    try:
        config_path = root / "TESTWP.yaml"
        config_path.write_text("key: TESTWP\n", encoding="utf-8")
        captured = {}
        def commit_file(*args):
            captured["commit"] = args
            return "commit0001"

        gateway = SimpleNamespace(default_branch=lambda _name: "develop", commit_file=commit_file)
        ctx = SimpleNamespace(settings=SimpleNamespace(control_repo="/repos/aiflow-control"))
        repos = {"z": {"repo": "z"}, "a": {"repo": "a"}}
        normalized, commit = project_routes.commit_project_repository_settings(
            ctx, config_path, "TESTWP", repos, {"TESTWP-12": "a"}, lambda _ctx: gateway)
        assert [item["repo"] for item in normalized] == ["a", "z"]
        assert captured["commit"][:3] == ("aiflow-control", "develop", "projects/TESTWP.yaml")
        assert yaml.safe_load(captured["commit"][3])["epic_repositories"] == {"TESTWP-12": "a"}
        assert commit == "commit0001"
    finally:
        shutil.rmtree(root, ignore_errors=True)
