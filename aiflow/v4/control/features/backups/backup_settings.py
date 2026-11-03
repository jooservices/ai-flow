"""Reading and validating the backup settings behind the backups routes (docs/v4/backups-install-structure.md)."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

from ....core import backups as control_backups
from .bodies import BackupConfigBody


def read_backup_config(ctx, include_history: bool = True) -> dict:
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


def folder_destination(ctx, body: BackupConfigBody) -> str:
    """The resolved folder of a local or mounted destination, which must stay clear of the control repository and secrets."""
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
    return str(destination)


def s3_destination(body: BackupConfigBody) -> tuple[str, str, str, str]:
    """(endpoint, bucket, prefix, region) of an S3-compatible destination."""
    parsed = urlsplit(body.s3_endpoint.strip())
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not body.s3_bucket.strip()):
        raise HTTPException(400, "enter an HTTPS S3-compatible endpoint and bucket")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,62}", body.s3_bucket.strip()):
        raise HTTPException(400, "S3 bucket name is invalid")
    endpoint, bucket = body.s3_endpoint.strip().rstrip("/"), body.s3_bucket.strip()
    prefix = "/".join(part for part in body.s3_prefix.strip().split("/") if part and part not in {".", ".."})
    region = body.s3_region.strip() or "us-east-1"
    return endpoint, bucket, prefix, region


def destination_values(ctx, body: BackupConfigBody) -> tuple[str, str, str, str, str]:
    """(path, endpoint, bucket, prefix, region) to store for the requested destination."""
    if body.destination_kind in {"local", "mounted"}:
        return folder_destination(ctx, body), "", "", "", "us-east-1"
    endpoint, bucket, prefix, region = s3_destination(body)
    return "/srv/aiflow/backups", endpoint, bucket, prefix, region


def create_backup_key(ctx) -> bytes:
    """Create the one-time encryption key file and record the download, or refuse when it was already downloaded."""
    key_path = Path(ctx.settings.backup_key_file)
    try:
        with ctx.db.pool.connection() as connection, connection.transaction():
            row = connection.execute("SELECT key_downloaded_at FROM control_backup_config WHERE id=true FOR UPDATE").fetchone()
            if not row:
                raise HTTPException(503, "backup settings are not initialized")
            if row["key_downloaded_at"] is not None or key_path.exists():
                raise HTTPException(409, "the backup encryption key has already been downloaded")
            key = control_backups.create_key_file(key_path)
            connection.execute("UPDATE control_backup_config SET key_downloaded_at=now(),updated_at=now() WHERE id=true")
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(503, "the backup encryption key could not be created") from exc
    return key
