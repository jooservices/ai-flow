"""Console user creation route."""
from types import SimpleNamespace

from fastapi import HTTPException, Request

from ... import licensing
from ...audit import record_console_user_change as audit_user
from ...console_auth import normalize_roles
from . import routes as console_routes
from .bodies import ConsoleUserCreateBody
from .events import ConsoleUserAdded


def install_console_user_create_routes(app, ctx, deps, validate_project_keys) -> SimpleNamespace:
    from ...api import log  # noqa: PLC0415 - api imports this module

    console_user = deps.console_user
    require_capacity = deps.require_capacity

    @app.post("/v1/console/users")
    def add_console_user(body: ConsoleUserCreateBody, request: Request) -> dict:
        user = console_user(request, "console.manage", action="user_add")
        from ...login import generate_password, hash_password, normalize_email
        try:
            email = normalize_email(body.email)
            roles = normalize_roles(body.roles)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        project_keys = validate_project_keys(body.project_keys)
        password = generate_password()
        try:
            with ctx.db.transaction() as connection, connection.transaction():
                licensing.lock_resource_admission(connection)
                people = connection.execute(
                    "SELECT count(*) AS n FROM console_users WHERE disabled=false").fetchone()["n"]
                require_capacity("users", int(people), connection=connection)
                created = connection.execute(
                    "INSERT INTO console_users (email,display_name,role,roles,project_keys,password_hash)"
                    " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING RETURNING email",
                    (email, body.display_name.strip()[:200], roles[0], list(roles), project_keys,
                     hash_password(password))).fetchone()
                if not created:
                    raise HTTPException(409, "a console user with that email already exists")
                console_routes.publish_for(ctx, ConsoleUserAdded(actor=user.subject, email=email, roles=list(roles),
                                                                 project_keys=project_keys))
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("console user could not be added")
            raise HTTPException(500, "person could not be added") from exc
        audit_user(request, user.subject, "person_added", email, {"roles": list(roles), "project_keys": project_keys})
        return {"email": email, "temporary_password": password}

    return SimpleNamespace(add_console_user=add_console_user)
