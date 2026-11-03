"""Console user update and disable routes."""
from types import SimpleNamespace

from fastapi import HTTPException, Request

from ...audit import record_console_user_change as audit_user, record_console_user_roles_change as audit_user_roles
from ...console_auth import normalize_roles
from . import routes as console_routes
from .bodies import ConsoleUserDisabledBody, ConsoleUserUpdateBody
from .events import ConsoleUserDisabledChanged, ConsoleUserUpdated


def install_console_user_change_routes(app, ctx, deps, validate_project_keys) -> SimpleNamespace:
    console_user = deps.console_user

    @app.put("/v1/console/users/{email}")
    def update_console_user(email: str, body: ConsoleUserUpdateBody, request: Request) -> dict:
        user = console_user(request, "console.manage", target="console", action="user_update")
        from ...login import normalize_email
        try:
            email = normalize_email(email)
            roles = normalize_roles(body.roles)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        project_keys = validate_project_keys(body.project_keys)
        with ctx.db.transaction() as connection:
            connection.execute("LOCK TABLE console_users IN EXCLUSIVE MODE")
            current = connection.execute("SELECT roles,disabled FROM console_users WHERE email=%s FOR UPDATE",
                                         (email,)).fetchone()
            if not current:
                raise HTTPException(404, "console user not found")
            roles_before = normalize_roles(current["roles"])
            if not current["disabled"] and "administrator" in roles_before and "administrator" not in roles:
                others = connection.execute("SELECT count(*) AS n FROM console_users WHERE email<>%s AND disabled=false"
                                            " AND 'administrator'=ANY(roles)", (email,)).fetchone()["n"]
                if int(others) == 0:
                    raise HTTPException(409, "the last administrator cannot be removed")
            connection.execute(
                "UPDATE console_users SET role=%s,roles=%s,project_keys=%s,updated_at=now() WHERE email=%s",
                (roles[0], list(roles), project_keys, email))
            console_routes.publish_for(ctx, ConsoleUserUpdated(actor=user.subject, email=email, roles=list(roles),
                                                               project_keys=project_keys))
        audit_user_roles(request, user.subject, email, roles_before, roles, project_keys)
        return {"updated": True}

    @app.patch("/v1/console/users/{email}/disabled")
    def set_console_user_disabled(email: str, body: ConsoleUserDisabledBody, request: Request) -> dict:
        user = console_user(request, "console.manage", target="console",
                            action="user_disable" if body.disabled else "user_enable")
        from ...login import normalize_email
        try:
            email = normalize_email(email)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with ctx.db.transaction() as connection:
            connection.execute("LOCK TABLE console_users IN EXCLUSIVE MODE")
            current = connection.execute("SELECT roles,disabled FROM console_users WHERE email=%s FOR UPDATE",
                                         (email,)).fetchone()
            if not current:
                raise HTTPException(404, "console user not found")
            roles = normalize_roles(current["roles"])
            if body.disabled and not current["disabled"] and "administrator" in roles:
                others = connection.execute("SELECT count(*) AS n FROM console_users WHERE email<>%s AND disabled=false"
                                            " AND 'administrator'=ANY(roles)", (email,)).fetchone()["n"]
                if int(others) == 0:
                    raise HTTPException(409, "the last administrator cannot be disabled")
            connection.execute("UPDATE console_users SET disabled=%s,updated_at=now() WHERE email=%s",
                               (body.disabled, email))
            console_routes.publish_for(
                ctx, ConsoleUserDisabledChanged(actor=user.subject, email=email, disabled=body.disabled))
        audit_user(request, user.subject, "person_disabled" if body.disabled else "person_enabled", email,
                   {"disabled": body.disabled})
        return {"disabled": body.disabled}

    return SimpleNamespace(update_console_user=update_console_user,
                           set_console_user_disabled=set_console_user_disabled)
