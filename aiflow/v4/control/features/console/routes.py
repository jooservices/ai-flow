"""Routes of the console feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from types import SimpleNamespace

from .bodies import ConsoleUserCreateBody, ConsoleUserDisabledBody, ConsoleUserUpdateBody, LicenseKeyBody, OfflineLicenseBody, PlanLimitsBody, ViewAsBody
from .events import (LicenceActivated, LicenceLimitsUpdated, NotificationChannelDeleted, NotificationChannelSaved,
                     NotificationPreferencesSaved, ViewAsStarted, ViewAsStopped)
from fastapi import HTTPException, Request
from fastapi.responses import (JSONResponse, PlainTextResponse)
from ...audit import record_console_user_change as audit_user, record_console_user_roles_change as audit_user_roles
from ... import (layouts, licensing)
from ...console_auth import (CANONICAL_ROLES, ConsoleUser, COOKIE_NAME, ROLE_LABELS, ROLE_PERMISSIONS, issue_session, landing_page, normalize_role, normalize_roles, resolve_session_user,
                           verify_session)
from ... import notify
from ...features.work.constants import utcnow
from ...platform.events.wiring import publish_for


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import log  # noqa: PLC0415 - api imports this module
    audit_in = deps.audit_in
    console_user = deps.console_user
    optional_console_user = deps.optional_console_user
    require_capacity = deps.require_capacity
    # --- health ---------------------------------------------------------------------------------------------
    @app.get("/v1/console/session")
    def console_session(request: Request) -> dict:
        user = (console_user(request, None) if ctx.settings.console_login else optional_console_user(request))
        if user is None:
            return {"authenticated": False}
        effective = user.view_as or user.role
        return {"authenticated": True, "user": user.subject, "role": effective,
                "role_label": ROLE_LABELS[effective], "roles": list(user.assigned_roles),
                "role_labels": [ROLE_LABELS[role] for role in user.assigned_roles],
                "permissions": sorted(user.permissions), "landing_page": landing_page((effective,)),
                "can_view_as": user.is_administrator, "viewing_as": user.view_as,
                "role_options": [{"id": role, "label": ROLE_LABELS[role],
                                  "permissions": sorted(ROLE_PERMISSIONS[role]),
                                  "landing_page": landing_page((role,))} for role in CANONICAL_ROLES]}

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
            with ctx.db.pool.connection() as connection:
                with connection.transaction():
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
                    audit_in(connection, "console", "user_add", "", "", None, user.subject, True, "Person added",
                             {"email": email, "roles": list(roles), "project_keys": project_keys})
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("console user could not be added")
            raise HTTPException(500, "person could not be added") from exc
        audit_user(request, user.subject, "person_added", email, {"roles": list(roles), "project_keys": project_keys})
        return {"email": email, "temporary_password": password}

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
        with ctx.db.pool.connection() as connection:
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
            connection.execute("UPDATE console_users SET role=%s,roles=%s,project_keys=%s,updated_at=now() WHERE email=%s",
                               (roles[0], list(roles), project_keys, email))
            audit_in(connection, "console", "user_update", "", "", None, user.subject, True, "Person updated",
                     {"email": email, "roles": list(roles), "project_keys": project_keys})
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
        with ctx.db.pool.connection() as connection:
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
            action = "user_disable" if body.disabled else "user_enable"
            audit_in(connection, "console", action, "", "", None, user.subject, True,
                     "Person disabled" if body.disabled else "Person enabled", {"email": email})
        audit_user(request, user.subject, "person_disabled" if body.disabled else "person_enabled", email, {"disabled": body.disabled})
        return {"disabled": body.disabled}

    @app.post("/v1/console/view-as")
    def console_view_as(body: ViewAsBody, request: Request) -> JSONResponse:
        user = console_user(request, "console.manage", target="console", action="view_as")
        try:
            role = normalize_role(body.role)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        from ...login import SESSION_TTL_S
        token = issue_session(user.subject, user.assigned_roles, int(utcnow().timestamp()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret, view_as=role, sid=user.sid)
        publish_for(ctx, ViewAsStarted(actor=user.subject, role=role))
        response = JSONResponse({"viewing_as": role, "role_label": ROLE_LABELS[role]})
        app.state.set_session_cookies(response, token, SESSION_TTL_S)  # keeps the session id and the CSRF token in step
        return response

    @app.post("/v1/console/view-as/stop")
    def console_stop_view_as(request: Request) -> JSONResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user or not user.is_administrator:
            raise HTTPException(403, "not available for your role")
        from ...login import SESSION_TTL_S
        token = issue_session(user.subject, user.assigned_roles, int(utcnow().timestamp()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret, sid=user.sid)
        publish_for(ctx, ViewAsStopped(actor=user.subject, previous_role=user.view_as or ""))
        response = JSONResponse({"viewing_as": None})
        app.state.set_session_cookies(response, token, SESSION_TTL_S)
        return response


    # --- per-person console layouts (layouts.py, docs/v4/console-layouts.md) ---------------------------------
    def layout_page(page: str) -> str:
        if page not in layouts.PAGES:
            raise HTTPException(404, "unknown console page")
        return page

    @app.get("/v1/console/layouts")
    def console_layouts(request: Request) -> dict:
        user = console_user(request, None)
        return layouts.overview(ctx.db, user.subject, user.role)

    @app.put("/v1/console/layouts/{page}")
    def console_layout_save(page: str, body: layouts.LayoutBody, request: Request) -> dict:
        # Layouts are personal preferences, like reads, and remain available if the licence expires.
        user = console_user(request, None)
        try:
            blocks = layouts.normalise(layout_page(page), [b.model_dump() for b in body.blocks])
        except layouts.LayoutError as exc:
            raise HTTPException(400, str(exc)) from None
        layouts.save(ctx.db, user.subject, page, blocks)
        return layouts.page_view(page, user.role, blocks)

    @app.delete("/v1/console/layouts/{page}")
    def console_layout_reset(page: str, request: Request) -> dict:
        user = console_user(request, None)
        layouts.reset(ctx.db, user.subject, layout_page(page))
        return layouts.page_view(page, user.role, None)

    @app.get("/v1/console/preferences")
    def console_preferences(request: Request) -> dict:
        user = console_user(request, None)
        row = ctx.db.one("SELECT theme FROM console_preferences WHERE email=%s", (user.subject,))
        return {"theme": row["theme"] if row else "dark"}

    @app.put("/v1/console/preferences")
    async def console_preferences_save(request: Request) -> dict:
        user = console_user(request, None)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "invalid console preferences") from None
        if not isinstance(body, dict) or body.get("theme") not in {"dark", "light"}:
            raise HTTPException(400, "theme must be dark or light")
        ctx.db.run("INSERT INTO console_preferences (email, theme) VALUES (%s, %s)"
                   " ON CONFLICT (email) DO UPDATE SET theme=EXCLUDED.theme, updated_at=now()",
                   (user.subject, body["theme"]))
        return {"theme": body["theme"]}

    def notification_admin(request: Request, action: str = "read") -> ConsoleUser:
        return console_user(request, {"notifications.manage"}, target="notifications", action=action)

    async def notification_json(request: Request) -> dict:
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "invalid notification settings") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "invalid notification settings")
        return payload

    @app.get("/v1/notifications")
    def notification_settings(request: Request) -> dict:
        notification_admin(request)
        projects = [{"key": project.key, "name": project.key}
                    for project in sorted(ctx.repo().projects.values(), key=lambda item: item.key)]
        return notify.public_settings(ctx.db, projects)

    @app.put("/v1/notifications/channels")
    async def notification_channel_save(request: Request) -> dict:
        user = notification_admin(request, "write")
        payload = await notification_json(request)
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        if str(payload.get("kind") or "").strip().lower() == "telegram" and config.get("ba_chat_enabled"):
            project_key = str(config.get("project_key") or "").strip().upper()
            if project_key not in ctx.repo().projects:
                raise HTTPException(400, "choose a configured Jira project for BA chat")
        try:
            channel = notify.save_channel(ctx.db, payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        publish_for(ctx, NotificationChannelSaved(actor=user.subject, channel_id=channel["id"],
                                                   kind=channel["kind"], enabled=channel["enabled"]))
        return {"channel": channel}

    @app.delete("/v1/notifications/channels/{channel_id}")
    def notification_channel_delete(channel_id: int, request: Request) -> dict:
        user = notification_admin(request, "write")
        if not notify.delete_channel(ctx.db, channel_id):
            raise HTTPException(404, "notification channel was not found")
        publish_for(ctx, NotificationChannelDeleted(actor=user.subject, channel_id=channel_id))
        return {"deleted": True}

    @app.put("/v1/notifications/preferences")
    async def notification_preferences_save(request: Request) -> dict:
        user = notification_admin(request, "write")
        try:
            preferences = notify.save_preferences(ctx.db, await notification_json(request))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        publish_for(ctx, NotificationPreferencesSaved(
            actor=user.subject, route_count=sum(len(ids) for ids in preferences["routes"].values()),
            quiet_hours_enabled=bool(preferences["quiet_start"] and preferences["quiet_end"])))
        return {"preferences": preferences}

    @app.post("/v1/notifications/channels/{channel_id}/test")
    def notification_channel_test(channel_id: int, request: Request) -> dict:
        notification_admin(request, "write")
        try:
            return notify.send_test(ctx, channel_id)
        except ValueError:
            raise HTTPException(404, "notification channel was not found") from None


    # --- licence and metered usage ---------------------------------------------------------------------------
    @app.get("/v1/licensing")
    def licensing_state(request: Request) -> dict:
        console_user(request, "licensing.manage")
        state = licensing.snapshot(ctx.db, ctx.settings.license_public_key)
        state["usage"] = licensing.resource_counts(ctx)
        state["sync_warnings"] = list(app.state.license_sync_warnings)
        return state

    @app.get("/v1/licensing/status")
    def licensing_status(request: Request) -> dict:
        console_user(request, "console.read")
        return licensing.status_fields(licensing.snapshot(ctx.db, ctx.settings.license_public_key))

    @app.post("/v1/licensing/activate")
    def activate_license(body: LicenseKeyBody, request: Request) -> dict:
        user = console_user(request, "licensing.manage", target="licensing", action="license_activate")
        try:
            state = licensing.activate_online(ctx.db, body.key, ctx.settings.license_service_url,
                                              ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None
        publish_for(ctx, LicenceActivated(actor=user.subject, source="online", plan=state["plan"]))
        return state

    @app.post("/v1/licensing/offline")
    def activate_license_offline(body: OfflineLicenseBody, request: Request) -> dict:
        user = console_user(request, "licensing.manage", target="licensing", action="license_activate")
        try:
            state = licensing.activate(ctx.db, body.license_file, ctx.settings.license_public_key, "offline")
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None
        publish_for(ctx, LicenceActivated(actor=user.subject, source="offline", plan=state["plan"]))
        return state

    @app.put("/v1/licensing/limits")
    def update_license_limits(body: PlanLimitsBody, request: Request) -> dict:
        user = console_user(request, "licensing.manage", target="licensing", action="license_limits")
        try:
            state = licensing.save_limits(ctx.db, body.plan, body.limits, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None
        publish_for(ctx, LicenceLimitsUpdated(actor=user.subject, plan=body.plan, limits=body.limits))
        return state

    @app.get("/v1/licensing/usage")
    def licensing_usage(request: Request) -> dict:
        console_user(request, "console.read")
        return {"rows": licensing.usage_report(ctx.db)}

    @app.get("/v1/licensing/usage.csv")
    def licensing_usage_csv(request: Request) -> PlainTextResponse:
        console_user(request, "console.read")
        content = licensing.usage_csv(licensing.usage_report(ctx.db))
        return PlainTextResponse(content, media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=ai-flow-usage.csv"})

    return SimpleNamespace(console_session=console_session, validate_project_keys=validate_project_keys, console_users=console_users, add_console_user=add_console_user, update_console_user=update_console_user, set_console_user_disabled=set_console_user_disabled, console_view_as=console_view_as, console_stop_view_as=console_stop_view_as, layout_page=layout_page, console_layouts=console_layouts, console_layout_save=console_layout_save, console_layout_reset=console_layout_reset, console_preferences=console_preferences, notification_admin=notification_admin, notification_settings=notification_settings, notification_channel_delete=notification_channel_delete, notification_channel_test=notification_channel_test, licensing_state=licensing_state, licensing_status=licensing_status, activate_license=activate_license, activate_license_offline=activate_license_offline, update_license_limits=update_license_limits, licensing_usage=licensing_usage, licensing_usage_csv=licensing_usage_csv)
