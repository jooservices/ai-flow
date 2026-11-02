"""The security screen's settings routes: session length and the two-factor requirement."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..core import secretbox
from . import audit as audit_log
from .login import (MFA_ROLES, SESSION_TTL_MAX_S, SESSION_TTL_MIN_S, get_setting, require_mfa, session_ttl_s,
                    set_setting)
from .security_guard import SecurityKit


class SettingsBody(BaseModel):
    session_ttl_s: int | None = Field(default=None, ge=SESSION_TTL_MIN_S, le=SESSION_TTL_MAX_S)
    require_mfa: bool | None = None


def key_configured() -> bool:
    try:
        return secretbox.load_key() is not None
    except secretbox.SecretBoxError:
        return False


def settings_view(kit: SecurityKit) -> dict:
    settings = kit.settings
    return {"session_ttl_s": session_ttl_s(kit.ctx), "session_ttl_min_s": SESSION_TTL_MIN_S,
            "session_ttl_max_s": SESSION_TTL_MAX_S, "require_mfa": require_mfa(kit.ctx), "mfa_roles": list(MFA_ROLES),
            "rate_limit_per_min": settings.rate_limit_per_min,
            "rate_limit_public_per_min": settings.rate_limit_public_per_min,
            "secret_key_configured": key_configured()}


def requested_changes(body: SettingsBody) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if body.session_ttl_s is not None:
        changes["session_ttl_s"] = body.session_ttl_s
    if body.require_mfa is not None:
        changes["require_mfa"] = body.require_mfa
    return changes


def setting_before(kit: SecurityKit, name: str):
    if name == "require_mfa":
        return get_setting(kit.ctx.db, "require_mfa", "1" if kit.settings.console_require_mfa else "0") == "1"
    return settings_view(kit)[name]


def apply_setting_changes(kit: SecurityKit, request: Request, subject: str, changes: dict[str, Any]) -> None:
    for name, value in changes.items():
        before = setting_before(kit, name)
        set_setting(kit.ctx.db, name, ("1" if value else "0") if isinstance(value, bool) else str(value), subject)
        audit_log.record(kit.ctx.db, subject, "settings", f"security_setting_{name}", name,
                         {"from": before, "to": value}, kit.ip_of(request))


def install_security_settings(kit: SecurityKit) -> None:
    app = kit.app

    @app.get("/v1/security/settings")
    def get_security_settings(request: Request) -> dict:
        kit.require(request, "security.manage")
        return settings_view(kit)

    @app.put("/v1/security/settings")
    def put_security_settings(request: Request, body: SettingsBody) -> dict:
        user = kit.require(request, "security.manage")
        if body.require_mfa is False:
            raise HTTPException(422, "two-factor sign-in is mandatory for administrator roles")
        apply_setting_changes(kit, request, user.subject, requested_changes(body))
        return settings_view(kit)
