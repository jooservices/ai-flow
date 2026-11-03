"""Routes of the backups feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from types import SimpleNamespace

from . import backup_settings
from .bodies import BackupConfigBody
from .events import BackupConfigured, BackupKeyDownloaded, BackupRunRequested
from fastapi import Request
from fastapi.responses import (StreamingResponse)
from ...platform.events.wiring import publish_for
from ....core import backups as control_backups  # noqa: F401 - tests patch routes.control_backups


def install(app, ctx, deps) -> SimpleNamespace:
    console_user = deps.console_user
    # --- encrypted backups and disaster recovery ----------------------------------------------------------
    def backup_config(include_history: bool = True) -> dict:
        return backup_settings.read_backup_config(ctx, include_history)

    @app.get("/v1/backups")
    def backup_status(request: Request) -> dict:
        console_user(request, "backup.read")
        return backup_config()

    @app.put("/v1/backups/config")
    def backup_configure(body: BackupConfigBody, request: Request) -> dict:
        user = console_user(request, "backup.manage", target="backup", action="configure")
        path_value, endpoint, bucket, prefix, region = backup_settings.destination_values(ctx, body)
        with ctx.db.transaction():
            ctx.db.run("UPDATE control_backup_config SET schedule=%s,destination_kind=%s,destination_path=%s,s3_endpoint=%s,"
                       "s3_bucket=%s,s3_prefix=%s,s3_region=%s,retention_days=%s,updated_at=now() WHERE id=true",
                       (body.schedule, body.destination_kind, path_value, endpoint, bucket, prefix, region,
                        body.retention_days))
            publish_for(ctx, BackupConfigured(actor=user.subject, schedule=body.schedule,
                                               destination=body.destination_kind,
                                               retention_days=body.retention_days))
        return backup_config()

    @app.post("/v1/backups/key")
    def backup_key_download(request: Request) -> StreamingResponse:
        user = console_user(request, "backup.manage", target="backup", action="download_key")
        key = backup_settings.create_backup_key(ctx)
        publish_for(ctx, BackupKeyDownloaded(actor=user.subject))
        return StreamingResponse(iter([key]), media_type="application/octet-stream",
                                  headers={"Content-Disposition": 'attachment; filename="aiflow4-backup-key"',
                                           "Cache-Control": "no-store", "Pragma": "no-cache",
                                           "X-Content-Type-Options": "nosniff"})

    @app.post("/v1/backups/run")
    def backup_request(request: Request) -> dict:
        user = console_user(request, "backup.manage", target="backup", action="run_now")
        with ctx.db.transaction():
            ctx.db.run("UPDATE control_backup_config SET backup_requested_at=now(),updated_at=now() WHERE id=true")
            publish_for(ctx, BackupRunRequested(actor=user.subject))
        return {"queued": True}

    return SimpleNamespace(backup_config=backup_config, backup_status=backup_status, backup_configure=backup_configure, backup_key_download=backup_key_download, backup_request=backup_request)
