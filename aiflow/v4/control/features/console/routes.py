"""Route-group entry point for the console feature."""
from types import SimpleNamespace

from ...platform.events.wiring import publish_for as _publish_for


def publish_for(ctx, event):
    """Keep the historic monkeypatch point while route groups call it at request time."""
    return _publish_for(ctx, event)


def install(app, ctx, deps) -> SimpleNamespace:
    from .routes_console_read import install_console_read_routes  # noqa: PLC0415
    from .routes_console_user_create import install_console_user_create_routes  # noqa: PLC0415
    from .routes_console_user_change import install_console_user_change_routes  # noqa: PLC0415
    from .routes_view_as import install_view_as_routes  # noqa: PLC0415
    from .routes_layouts import install_layout_preference_routes  # noqa: PLC0415
    from .routes_notifications import install_notification_routes  # noqa: PLC0415
    from .routes_licensing import install_licensing_routes  # noqa: PLC0415

    console = install_console_read_routes(app, ctx, deps)
    user_create = install_console_user_create_routes(app, ctx, deps, console.validate_project_keys)
    user_change = install_console_user_change_routes(app, ctx, deps, console.validate_project_keys)
    view_as = install_view_as_routes(app, ctx, deps)
    layouts = install_layout_preference_routes(app, ctx, deps)
    notifications = install_notification_routes(app, ctx, deps)
    licensing = install_licensing_routes(app, ctx, deps)
    return SimpleNamespace(**vars(console), **vars(user_create), **vars(user_change), **vars(view_as),
                           **vars(layouts), **vars(notifications), **vars(licensing))
