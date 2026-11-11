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
from aiflow.v4.control.eligibility import ProviderRoutingEligibilityService
from aiflow.v4.control.events import Events
from aiflow.v4.control.repositories.accounts import AccountRepository, FakeAccountRepository
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
    assert isinstance(ctx.account_repository, AccountRepository)
    assert ctx.account_repository.db is ctx.db
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


def test_application_context_retains_falsey_account_repository_and_defaults_only_on_none(ctx):
    class FalseyAccountRepository(FakeAccountRepository):
        def __bool__(self):
            return False

    fake = FalseyAccountRepository()
    assert not fake
    context = replace(ctx, account_repository=fake)
    assert context.account_repository is fake

    defaulted = replace(context, account_repository=None)
    assert isinstance(defaulted.account_repository, AccountRepository)
    assert defaulted.account_repository.db is defaulted.db


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


def test_eligibility_service_uses_its_database_account_repository(ctx):
    rows = (
        ("jaif301-charlie", "codex", "working", '{"quota":1}', True),
        ("jaif301-alpha", "claude", "idle", '{"quota":2}', False),
        ("jaif301-bravo", "codex", "paused", '{"quota":3}', False),
        ("jaif301-delta", "grok", "probing", '{"quota":4}', False),
        ("jaif301-echo", "grok", "signed_out", '{"quota":5}', False),
    )
    for account_id, provider, status, quota, at_risk in rows:
        ctx.db.run("INSERT INTO accounts (id,provider,status,quota,quota_at_risk)"
                   " VALUES (%s,%s,%s,%s::jsonb,%s)",
                   (account_id, provider, status, quota, at_risk))

    service = ProviderRoutingEligibilityService(ctx, None, lambda *args, **kwargs: [], lambda account: False,
                                                lambda account, roles: [])
    assert isinstance(service.account_repository, AccountRepository)
    assert service.account_repository.db is ctx.db
    assert service.account_repository.quota_routing_rows(["grok", "codex", "grok"]) == [
        {"id": "jaif301-bravo", "provider": "codex", "quota": {"quota": 3}, "quota_at_risk": False},
        {"id": "jaif301-charlie", "provider": "codex", "quota": {"quota": 1}, "quota_at_risk": True},
        {"id": "jaif301-delta", "provider": "grok", "quota": {"quota": 4}, "quota_at_risk": False},
        {"id": "jaif301-echo", "provider": "grok", "quota": {"quota": 5}, "quota_at_risk": False},
    ]
    assert service.account_repository.ready_ids() == {"jaif301-charlie", "jaif301-alpha", "jaif301-delta"}


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
