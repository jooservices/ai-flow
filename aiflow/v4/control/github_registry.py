"""Lookups of the configured GitHub connections, below the API routes and the setup service."""
from __future__ import annotations

from pathlib import Path

from .github import GitHubApp
from .github_connect import GitHubSecretStore
from .github_connections import GitHubConnections, GitHubTokenApp


def github_connections(ctx, app_factory=GitHubApp, token_factory=GitHubTokenApp) -> GitHubConnections:
    """The registry of the connections after the default one, shared by every API replica through the secret store.

    The factories are looked up by the caller at call time, so the API can swap its gateway classes."""
    registry = getattr(ctx, "github_registry", None)
    if registry is None or registry.root != Path(ctx.settings.github_app_dir):
        registry = GitHubConnections(ctx.settings.github_app_dir, lambda directory, org: app_factory(directory, org),
                                     lambda token, account, kind: token_factory(token, account, kind))
        ctx.github_registry = registry
    return registry


def github_connected(ctx) -> bool:
    """Whether the default App or any further connection is configured."""
    gateway = ctx.github
    if gateway is not None:
        return bool(getattr(gateway, "configured", True))
    if GitHubSecretStore(ctx.settings.github_app_dir).active_exists:
        return True
    return bool(github_connections(ctx).connections())
