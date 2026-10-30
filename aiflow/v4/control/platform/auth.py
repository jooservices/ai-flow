"""`can(permission)`: the route dependency that says who may call a route.

It replaces the repeated `user = console_user_for(request, "view.x") if ctx.settings.console_login else None` line:
`def route(user: ConsoleUser | None = can("view.dashboard"))`. With console sign-in off it returns the session user if
one is present, else None (the open console); with sign-in on it requires a signed session whose role holds the permission, answers 401 or 403 otherwise,
writes the refusal to the audit log for a non-read action, and checks the licence for a write, exactly as
`route_deps.console_user` does."""
from __future__ import annotations

from fastapi import Depends, Request

from .. import route_deps
from ..console_auth import ConsoleUser


def can(permission: set[str] | str | None, *, target: str = "console", action: str = "read") -> ConsoleUser | None:
    """A FastAPI dependency (use it as a parameter default) requiring `permission` when console sign-in is on."""

    def dependency(request: Request) -> ConsoleUser | None:
        ctx = route_deps.context(request)
        if not ctx.settings.console_login:
            return route_deps.optional_console_user(ctx, request)
        return route_deps.console_user_for(request, permission, target, action)

    return Depends(dependency)
