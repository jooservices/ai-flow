"""Routes of the backups feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import re
from pathlib import Path
from types import SimpleNamespace

from .bodies import BackupConfigBody
from .events import BackupConfigured, BackupKeyDownloaded, BackupRunRequested
from fastapi import HTTPException, Request
from fastapi.responses import (StreamingResponse)
from ...platform.events.wiring import publish_for
from ....core import backups as control_backups


def install(app, ctx, deps) -> SimpleNamespace:
    console_user = deps.console_user
    # --- encrypted backups and disaster recovery ----------------------------------------------------------
    def backup_config(include_history: bool = True) -> dict:
        row = ctx.db.one("SELECT * FROM control_backup_config WHERE id=true")
        if not row:
            raise HTTPException(503, "backup settings are not initialized")
        result = {key: value for key, value in row.items() if key not in {"id", "updated_at"}}
        for key, value in list(result.items()):
            if hasattr(value, "isoformat"):
                result[key] = value.isoformat()
        result["key_downloaded"] = bool(row["key_downloaded_at"])
        result["key_available"] = Path(ctx.settings.backup_key_file).is_file()
        if include_history:
            try:
                result["backups"] = control_backups.list_backups(result)
                result["storage_error"] = ""
            except control_backups.BackupError:
                result["backups"] = []
                result["storage_error"] = "Backup destination could not be listed; check its settings and credentials."
        return result

    @app.get("/v1/backups")
    def backup_status(request: Request) -> dict:
        console_user(request, "backup.read")
        return backup_config()

    @app.put("/v1/backups/config")
    def backup_configure(body: BackupConfigBody, request: Request) -> dict:
        user = console_user(request, "backup.manage", target="backup", action="configure")
        if body.destination_kind in {"local", "mounted"}:
            requested = Path(body.destination_path.strip())
            if not requested.is_absolute() or requested == Path("/") or ".." in requested.parts:
                raise HTTPException(400, "backup folder must be an absolute folder path")
            destination = requested.resolve(strict=False)
            if body.destination_kind == "local":
                local_root = Path(ctx.settings.backup_path).resolve(strict=False)
                if destination != local_root and not destination.is_relative_to(local_root):
                    raise HTTPException(400, "local backups must be stored under the durable local backup folder")
            protected = [Path(ctx.settings.control_repo).resolve(strict=False),
                         Path(ctx.settings.control_secrets_dir).resolve(strict=False),
                         Path(ctx.settings.console_secrets_dir).resolve(strict=False),
                         Path(ctx.settings.github_app_dir).resolve(strict=False),
                         Path(ctx.settings.jira_secret_dir).resolve(strict=False)]
            if any(destination == root or destination.is_relative_to(root) or root.is_relative_to(destination)
                   for root in protected):
                raise HTTPException(400, "backup folder must be separate from the control repository and secrets")
            path_value = str(destination)
            endpoint, bucket, prefix, region = "", "", "", "us-east-1"
        else:
            from urllib.parse import urlsplit
            parsed = urlsplit(body.s3_endpoint.strip())
            if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not body.s3_bucket.strip()):
                raise HTTPException(400, "enter an HTTPS S3-compatible endpoint and bucket")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,62}", body.s3_bucket.strip()):
                raise HTTPException(400, "S3 bucket name is invalid")
            endpoint, bucket = body.s3_endpoint.strip().rstrip("/"), body.s3_bucket.strip()
            prefix = "/".join(part for part in body.s3_prefix.strip().split("/") if part and part not in {".", ".."})
            region = body.s3_region.strip() or "us-east-1"
            path_value = "/srv/aiflow/backups"
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
        from fastapi import HTTPException as ApiError
        key_path = Path(ctx.settings.backup_key_file)
        try:
            with ctx.db.pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute("SELECT key_downloaded_at FROM control_backup_config WHERE id=true FOR UPDATE").fetchone()
                    if not row:
                        raise ApiError(503, "backup settings are not initialized")
                    if row["key_downloaded_at"] is not None or key_path.exists():
                        raise ApiError(409, "the backup encryption key has already been downloaded")
                    key = control_backups.create_key_file(key_path)
                    connection.execute("UPDATE control_backup_config SET key_downloaded_at=now(),updated_at=now() WHERE id=true")
        except ApiError:
            raise
        except OSError as exc:
            raise HTTPException(503, "the backup encryption key could not be created") from exc
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
