"""Notification settings and channel routes."""
from types import SimpleNamespace

from fastapi import HTTPException, Request

from ... import notify
from ...console_auth import ConsoleUser
from . import routes as console_routes
from .events import NotificationChannelDeleted, NotificationChannelSaved, NotificationPreferencesSaved


async def notification_json(request: Request) -> dict:
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "invalid notification settings") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid notification settings")
    return payload


def install_notification_routes(app, ctx, deps) -> SimpleNamespace:
    console_user = deps.console_user

    def notification_admin(request: Request, action: str = "read") -> ConsoleUser:
        return console_user(request, {"notifications.manage"}, target="notifications", action=action)

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
        console_routes.publish_for(ctx, NotificationChannelSaved(actor=user.subject, channel_id=channel["id"],
                                                                  kind=channel["kind"], enabled=channel["enabled"]))
        return {"channel": channel}

    @app.delete("/v1/notifications/channels/{channel_id}")
    def notification_channel_delete(channel_id: int, request: Request) -> dict:
        user = notification_admin(request, "write")
        if not notify.delete_channel(ctx.db, channel_id):
            raise HTTPException(404, "notification channel was not found")
        console_routes.publish_for(ctx, NotificationChannelDeleted(actor=user.subject, channel_id=channel_id))
        return {"deleted": True}

    @app.put("/v1/notifications/preferences")
    async def notification_preferences_save(request: Request) -> dict:
        user = notification_admin(request, "write")
        try:
            preferences = notify.save_preferences(ctx.db, await notification_json(request))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        console_routes.publish_for(ctx, NotificationPreferencesSaved(
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

    return SimpleNamespace(notification_admin=notification_admin, notification_settings=notification_settings,
                           notification_channel_delete=notification_channel_delete,
                           notification_channel_test=notification_channel_test)
