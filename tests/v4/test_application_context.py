from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import get_type_hints

import pytest

from aiflow.v4.control import api, jira_connect, login, runtime_settings, setup_wizard, shared_accounts
from aiflow.v4.control.application_context import ApplicationContext, Clock, Ctx, SystemClock
from aiflow.v4.control.db import DB
from aiflow.v4.control.events import Events
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.core.fencing import Leases
from aiflow.v4.core.settings import Settings
from aiflow.v4.control.ui import CONSOLE_PAGE_PATHS


SNAPSHOT = Path(__file__).parent / "fixtures" / "application-context-route-snapshot.json"


def test_application_context_exposes_typed_dependencies(ctx):
    assert Ctx is ApplicationContext
    context_types = get_type_hints(ApplicationContext)
    assert context_types["settings"] is Settings
    assert context_types["db"] is DB
    assert context_types["leases"] is Leases
    assert context_types["events"] is Events
    assert context_types["repo"] == get_type_hints(ApplicationContext.repositories.fget)["return"]
    assert get_type_hints(ApplicationContext.database.fget)["return"] is DB
    assert ctx.database is ctx.db
    assert ctx.repositories is ctx.repo
    assert isinstance(ctx.repo(), ControlRepo)
    assert ctx.repo_configuration == ctx.settings.control_repo
    assert isinstance(ctx.clock, SystemClock)
    assert get_type_hints(ApplicationContext)["clock"] is Clock
    assert get_type_hints(ApplicationContext)["repo_configuration"] is str
    assert ctx.clock.now().tzinfo is not None
    assert replace(ctx, repo_configuration="").repo_configuration == ctx.settings.control_repo


def test_application_context_accepts_repository_and_clock_replacements(ctx):
    provider = ctx.repositories
    ctx.repositories = lambda: provider()
    assert ctx.repositories is not provider
    assert ctx.repositories() is provider()

    class FixedClock:
        def now(self):
            return datetime(2026, 10, 26, tzinfo=timezone.utc)

    fixed_clock = FixedClock()
    assert replace(ctx, clock=fixed_clock).clock.now() == fixed_clock.now()

    with pytest.raises(TypeError, match="repositories must be callable"):
        ctx.repositories = object()
    with pytest.raises(TypeError, match=r"clock must provide now\(\)"):
        ApplicationContext(settings=ctx.settings, db=ctx.db, leases=ctx.leases, events=ctx.events,
                            repo=ctx.repo, clock=object())


def test_application_context_preserves_legacy_positional_constructor(ctx):
    context = Ctx(ctx.settings, ctx.db, ctx.leases, ctx.events, ctx.repo, ctx.jira)

    assert context.jira is ctx.jira
    assert context.repo_configuration == ctx.settings.control_repo
    assert isinstance(context.clock, SystemClock)

    refresh_repo = lambda: None
    jira_auth = object()
    legacy_context = Ctx(ctx.settings, ctx.db, ctx.leases, ctx.events, ctx.repo, ctx.jira,
                         ctx.github, refresh_repo, jira_auth)

    assert legacy_context.jira is ctx.jira
    assert legacy_context.github is ctx.github
    assert legacy_context.refresh_repo is refresh_repo
    assert legacy_context.jira_auth is jira_auth
    assert legacy_context.repo_configuration == ctx.settings.control_repo
    assert isinstance(legacy_context.clock, SystemClock)


def test_application_context_requires_its_core_dependencies(ctx):
    with pytest.raises(TypeError, match="required positional arguments"):
        ApplicationContext(settings=ctx.settings)


def test_create_app_passes_the_same_context_to_feature_modules(ctx, monkeypatch):
    installer_targets = (
        ("setup_wizard.install", setup_wizard, "install"),
        ("shared_accounts.install", shared_accounts, "install"),
        ("jira_connect.mount", jira_connect, "mount"),
        ("runtime_settings.install", runtime_settings, "install"),
        ("login.install_login", login, "install_login"),
    )
    installers = tuple(getattr(module, attribute) for _, module, attribute in installer_targets)
    received = {name: [] for name, _, _ in installer_targets}

    for name, module, attribute in installer_targets:
        original = getattr(module, attribute)

        def capture(app, ctx, *args, _name=name, _original=original, **kwargs):
            received[_name].append(ctx)
            return _original(app, ctx, *args, **kwargs)

        monkeypatch.setattr(module, attribute, capture)

    app = api.create_app(ctx)

    assert app.state.ctx is ctx
    assert all(len(calls) == 1 and calls[0] is ctx for calls in received.values())
    assert all(get_type_hints(installer).get("ctx") is ApplicationContext for installer in installers)


def flatten_routes(routes):
    """The concrete routes, looking inside routers that were included through the router registry."""
    for route in routes:
        if hasattr(route, "methods"):
            yield route
        else:
            prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            for inner in flatten_routes(getattr(route, "original_router").routes):
                yield type("R", (), {"methods": inner.methods, "path": prefix + inner.path})()


def test_route_and_screen_lists_match_the_pre_change_snapshot(ctx):
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    app = api.create_app(ctx)
    routes = [f"{','.join(sorted(route.methods or ()))} {route.path}" for route in flatten_routes(app.routes)]

    # Order follows the route modules (JAIF-272 moves routes between files); the set of routes is what must not change.
    assert sorted(routes) == snapshot["routes"]
    assert list(CONSOLE_PAGE_PATHS) == snapshot["screens"]
