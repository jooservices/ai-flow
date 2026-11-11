"""Characterize the installed run-repository helpers before extracting them."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from aiflow.v4.control.app_helpers import install as install_app_helpers
from aiflow.v4.control import run_repository_helpers


_MISSING = object()


class RecordingProject:
    def __init__(self, trace, *, repository="org/default", settings_result=None, settings_error=None):
        self.trace = trace
        self.repository = repository
        self.settings_result = settings_result
        self.settings_error = settings_error

    def repo_for(self, ticket):
        self.trace.append(("repo_for", ticket))
        return self.repository

    def settings_for_repo(self, repository):
        self.trace.append(("settings_for_repo", repository))
        if self.settings_error:
            raise self.settings_error
        return self.settings_result


class RecordingTickets:
    def __init__(self, trace, *, ticket=None, ancestors=None, lookup_error=None):
        self.trace = trace
        self.ticket = ticket
        self.ancestors = ancestors or {}
        self.lookup_error = lookup_error

    def get_ticket_type_and_parent(self, params):
        self.trace.append(("ticket", params[0]))
        if self.lookup_error:
            raise self.lookup_error
        return self.ticket

    def ticket_ancestor(self, key):
        self.trace.append(("ancestor", key))
        return self.ancestors.get(key)


def _context(trace, *, ticket=None, ancestors=None, lookup_error=None):
    tickets = RecordingTickets(trace, ticket=ticket, ancestors=ancestors, lookup_error=lookup_error)
    return SimpleNamespace(
        ticket_repository=tickets,
        repo=lambda: trace.append(("repo",)) or SimpleNamespace(projects={}),
    )


def _installed(ctx):
    return install_app_helpers(None, ctx, SimpleNamespace())


def test_installed_settings_returns_saved_truthy_dict_without_lookups():
    trace = []
    settings = {"repo": "configured/repo", "base": "main"}
    project = RecordingProject(trace)
    ctx = _context(trace, lookup_error=AssertionError("repository lookup must not run"))

    result = _installed(ctx).settings_for_run(
        {"details": {"repository": "saved/repo", "repository_settings": settings}}, project
    )

    assert result is settings
    assert trace == []


@pytest.mark.parametrize(
    "repository_settings",
    [
        _MISSING,
        None,
        "not a dict",
        {},
        {"repo": ""},
        {"repo": None},
    ],
)
def test_installed_settings_falls_back_to_saved_repository(repository_settings):
    trace = []
    settings_result = {"repo": "org/saved", "base": "release"}
    project = RecordingProject(trace, settings_result=settings_result)
    ctx = _context(trace)
    details = {"repository": "org/saved"}
    if repository_settings is not _MISSING:
        details["repository_settings"] = repository_settings

    result = _installed(ctx).settings_for_run({"details": details}, project)

    assert result is settings_result
    assert trace == [("settings_for_repo", "org/saved")]


def test_installed_settings_fallback_resolves_ticket_epic_before_project_settings():
    trace = []
    project = RecordingProject(trace, repository="org/project")
    ctx = _context(
        trace,
        ticket={"key": "DEMO-1", "type": "Story", "parent_key": "DEMO-2"},
        ancestors={
            "DEMO-2": {"type": "Story", "parent_key": "DEMO-EPIC"},
            "DEMO-EPIC": {"type": "Epic", "parent_key": ""},
        },
    )
    settings_result = {"base": "main"}
    project.settings_result = settings_result

    result = _installed(ctx).settings_for_run({"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}, project)

    assert result is settings_result
    assert trace == [
        ("ticket", "DEMO-1"),
        ("ancestor", "DEMO-2"),
        ("ancestor", "DEMO-EPIC"),
        ("repo_for", {"key": "DEMO-1", "type": "Story", "parent_key": "DEMO-2", "epic": "DEMO-EPIC"}),
        ("settings_for_repo", "org/project"),
    ]


def test_installed_settings_propagates_repository_resolution_error_without_settings_call():
    trace = []
    error = LookupError("ticket repository failed")
    project = RecordingProject(trace)
    ctx = _context(trace, lookup_error=error)

    with pytest.raises(LookupError) as raised:
        _installed(ctx).settings_for_run({"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}, project)

    assert raised.value is error
    assert trace == [("ticket", "DEMO-1")]


def test_installed_settings_propagates_project_settings_error_after_resolution():
    trace = []
    error = RuntimeError("settings failed")
    project = RecordingProject(trace, settings_error=error)
    ctx = _context(trace)

    with pytest.raises(RuntimeError) as raised:
        _installed(ctx).settings_for_run({"details": {"repository": "org/saved"}}, project)

    assert raised.value is error
    assert trace == [("settings_for_repo", "org/saved")]


@pytest.mark.parametrize(
    ("repository", "requested", "expected"),
    [
        ("Org/Repo", " ORG/REPO ", True),
        ("Org/Repo", " RePo ", True),
        ("Org/Repo", "other/repo", False),
        ("Org/Repo", "org/other", False),
        ("", "", True),
        ("Org/Repo", "", False),
        ("Org/", "", True),
        ("Repo", " repo ", True),
        ("Org/team/repo", "TEAM/REPO", True),
        ("Org/team/repo", "repo", False),
    ],
)
def test_installed_repository_matching_keeps_current_string_rules(repository, requested, expected):
    assert _installed(SimpleNamespace()).matches_run_repository(repository, requested) is expected


def test_installed_repository_matching_preserves_non_string_request_error():
    with pytest.raises(AttributeError, match="'int' object has no attribute 'strip'"):
        _installed(SimpleNamespace()).matches_run_repository("org/repo", 1)


def test_installed_factory_namespace_and_helper_signatures_are_unchanged():
    helpers = _installed(SimpleNamespace())
    assert set(vars(helpers)) == {
        "bot_identity",
        "node_auth",
        "release_reader",
        "reviewer_auth",
        "owner_auth",
        "deploy_auth",
        "audit",
        "audit_in",
        "console_user",
        "optional_console_user",
        "require_capacity",
        "run_row",
        "fenced",
        "master_run",
        "reviewer_run",
        "finish_reviewer_run",
        "reviewer_comment",
        "reviewer_transition",
        "reviewer_labels",
        "record_reviewer_action",
        "repository_for_run",
        "settings_for_run",
        "matches_run_repository",
        "merge_command",
        "queued_merge",
        "master_budget_state",
        "account_master_paused",
        "mark_machine_auth_failure",
        "quota_forecast_at_risk",
        "refresh_quota_forecast",
        "quota_peer_accounts",
        "account_quota_at_risk",
        "quota_peer_release_candidates",
        "master_snapshot",
        "process_flow_bugs",
        "move_flow_bug_to_intake",
        "create_join_grant",
    }
    assert str(inspect.signature(install_app_helpers)) == "(app, ctx, deps) -> 'SimpleNamespace'"
    assert str(inspect.signature(helpers.repository_for_run)) == "(run: 'dict[str, Any]', project=None) -> 'str'"
    assert str(inspect.signature(helpers.settings_for_run)) == "(run: 'dict[str, Any]', project) -> 'dict[str, Any]'"
    assert str(inspect.signature(helpers.matches_run_repository)) == "(repository: 'str', requested: 'str') -> 'bool'"


def test_direct_settings_returns_saved_truthy_dict_without_lookups():
    trace = []
    settings = {"repo": "configured/repo", "base": "main"}
    project = RecordingProject(trace)
    ctx = _context(trace, lookup_error=AssertionError("repository lookup must not run"))

    result = run_repository_helpers.settings_for_run(
        ctx, {"details": {"repository": "saved/repo", "repository_settings": settings}}, project
    )

    assert result is settings
    assert trace == []


@pytest.mark.parametrize(
    "repository_settings",
    [
        _MISSING,
        None,
        "not a dict",
        {},
        {"repo": ""},
        {"repo": None},
    ],
)
def test_direct_settings_falls_back_to_saved_repository(repository_settings):
    trace = []
    settings_result = {"repo": "org/saved", "base": "release"}
    project = RecordingProject(trace, settings_result=settings_result)
    ctx = _context(trace)
    details = {"repository": "org/saved"}
    if repository_settings is not _MISSING:
        details["repository_settings"] = repository_settings

    result = run_repository_helpers.settings_for_run(ctx, {"details": details}, project)

    assert result is settings_result
    assert trace == [("settings_for_repo", "org/saved")]


def test_direct_settings_resolves_ticket_epic_before_project_settings():
    trace = []
    project = RecordingProject(trace, repository="org/project")
    ctx = _context(
        trace,
        ticket={"key": "DEMO-1", "type": "Story", "parent_key": "DEMO-2"},
        ancestors={
            "DEMO-2": {"type": "Story", "parent_key": "DEMO-EPIC"},
            "DEMO-EPIC": {"type": "Epic", "parent_key": ""},
        },
    )
    settings_result = {"base": "main"}
    project.settings_result = settings_result

    result = run_repository_helpers.settings_for_run(
        ctx, {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}, project
    )

    assert result is settings_result
    assert trace == [
        ("ticket", "DEMO-1"),
        ("ancestor", "DEMO-2"),
        ("ancestor", "DEMO-EPIC"),
        ("repo_for", {"key": "DEMO-1", "type": "Story", "parent_key": "DEMO-2", "epic": "DEMO-EPIC"}),
        ("settings_for_repo", "org/project"),
    ]


def test_direct_settings_propagates_repository_resolution_error_without_settings_call():
    trace = []
    error = LookupError("ticket repository failed")
    project = RecordingProject(trace)
    ctx = _context(trace, lookup_error=error)

    with pytest.raises(LookupError) as raised:
        run_repository_helpers.settings_for_run(
            ctx, {"project": "DEMO", "ticket_key": "DEMO-1", "details": {}}, project
        )

    assert raised.value is error
    assert trace == [("ticket", "DEMO-1")]


def test_direct_settings_propagates_project_settings_error_after_resolution():
    trace = []
    error = RuntimeError("settings failed")
    project = RecordingProject(trace, settings_error=error)
    ctx = _context(trace)

    with pytest.raises(RuntimeError) as raised:
        run_repository_helpers.settings_for_run(ctx, {"details": {"repository": "org/saved"}}, project)

    assert raised.value is error
    assert trace == [("settings_for_repo", "org/saved")]


@pytest.mark.parametrize(
    ("repository", "requested", "expected"),
    [
        ("Org/Repo", " ORG/REPO ", True),
        ("Org/Repo", " RePo ", True),
        ("Org/Repo", "other/repo", False),
        ("Org/Repo", "org/other", False),
        ("", "", True),
        ("Org/Repo", "", False),
        ("Org/", "", True),
        ("Repo", " repo ", True),
        ("Org/team/repo", "TEAM/REPO", True),
        ("Org/team/repo", "repo", False),
    ],
)
def test_direct_repository_matching_keeps_current_string_rules(repository, requested, expected):
    assert run_repository_helpers.matches_run_repository(repository, requested) is expected


def test_direct_repository_matching_preserves_non_string_request_error():
    with pytest.raises(AttributeError, match="'int' object has no attribute 'strip'"):
        run_repository_helpers.matches_run_repository("org/repo", 1)


def test_installed_helpers_delegate_to_the_shared_module(monkeypatch):
    trace = []
    settings = {"base": "main"}
    project = object()

    def settings_for_run(ctx, run, supplied_project):
        trace.append(("settings", ctx, run, supplied_project))
        return settings

    def matches_run_repository(repository, requested):
        trace.append(("matches", repository, requested))
        return True

    monkeypatch.setattr(run_repository_helpers, "settings_for_run", settings_for_run)
    monkeypatch.setattr(run_repository_helpers, "matches_run_repository", matches_run_repository)
    ctx = object()
    run = {"details": {}}
    helpers = _installed(ctx)

    assert helpers.settings_for_run(run, project) is settings
    assert helpers.matches_run_repository("org/repo", "repo") is True
    assert trace == [
        ("settings", ctx, run, project),
        ("matches", "org/repo", "repo"),
    ]
