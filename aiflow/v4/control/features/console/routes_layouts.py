"""Personal console layouts and theme preference routes."""
from types import SimpleNamespace

from fastapi import HTTPException, Request

from ... import layouts


def install_layout_preference_routes(app, ctx, deps) -> SimpleNamespace:
    console_user = deps.console_user

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

    return SimpleNamespace(layout_page=layout_page, console_layouts=console_layouts,
                           console_layout_save=console_layout_save, console_layout_reset=console_layout_reset,
                           console_preferences=console_preferences)
