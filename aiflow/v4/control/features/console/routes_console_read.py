"""Session and console user read routes."""
from types import SimpleNamespace

from fastapi import HTTPException, Request

from ...console_auth import CANONICAL_ROLES, ROLE_LABELS, ROLE_PERMISSIONS


def install_console_read_routes(app, ctx, deps) -> SimpleNamespace:
    console_user = deps.console_user

    def validate_project_keys(project_keys: list[str] | None) -> list[str] | None:
        if project_keys is None:
            return None
        selected = list(dict.fromkeys(str(key).strip() for key in project_keys if str(key).strip()))
        unknown = sorted(set(selected) - set(ctx.repo().projects))
        if unknown:
            raise HTTPException(400, "unknown project: " + ", ".join(unknown))
        return selected

    @app.get("/v1/console/users")
    def console_users(request: Request) -> dict:
        console_user(request, "console.manage")
        rows = ctx.db.all("SELECT email,display_name,roles,project_keys,disabled,last_login_at,created_at"
                          " FROM console_users ORDER BY lower(display_name),email")
        audits = ctx.db.all("SELECT ts,actor,action,accepted,result,payload FROM write_audit"
                            " WHERE target='console' AND action IN ('user_add','user_update','user_disable',"
                            "'user_enable','view_as','view_as_stop') ORDER BY id DESC LIMIT 100")
        return {"users": rows, "projects": sorted(ctx.repo().projects), "audit": audits,
                "role_options": [{"id": role, "label": ROLE_LABELS[role],
                                  "permissions": sorted(ROLE_PERMISSIONS[role])} for role in CANONICAL_ROLES]}

    return SimpleNamespace(validate_project_keys=validate_project_keys, console_users=console_users)
