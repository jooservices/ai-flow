"""Shared settings and matching helpers for fenced routes using run repositories."""

from __future__ import annotations

from typing import Any

from .app_helpers import repository_for_run


def settings_for_run(ctx: Any, run: dict[str, Any], project) -> dict[str, Any]:
    """Return saved run settings or look them up using the existing repository resolver."""
    settings = (run.get("details") or {}).get("repository_settings")
    if isinstance(settings, dict) and settings.get("repo"):
        return settings
    return project.settings_for_repo(repository_for_run(ctx, run, project))


def matches_run_repository(repository: str, requested: str) -> bool:
    """Match a trimmed request against the full repository or its name, ignoring case."""
    requested = requested.strip().lower()
    return requested in {repository.lower(), repository.split("/", 1)[-1].lower()}
