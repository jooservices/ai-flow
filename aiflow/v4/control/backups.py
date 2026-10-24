"""Scheduled encrypted backup and isolated weekly restore-test jobs for the control plane."""
from __future__ import annotations

import logging
import psycopg
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from psycopg import sql
from psycopg.conninfo import make_conninfo

from ..core import backups
from .incidents import close_incident, open_incident
from .db import J

log = logging.getLogger("aiflow4.backups")


def _config(ctx) -> dict | None:
    return ctx.db.one("SELECT * FROM control_backup_config WHERE id=true")


def _as_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _backup_is_due(config: dict, now: datetime) -> bool:
    last = _as_datetime(config.get("last_backup_at"))
    interval = timedelta(days=7 if config.get("schedule") == "weekly" else 1)
    return last is None or now - last >= interval


def scheduled_backup(ctx, now: datetime | None = None) -> dict | None:
    """Run one requested or due backup. Failures become one open backup incident until the next success."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    config = _config(ctx)
    if not config or not config.get("key_downloaded_at") or not Path(ctx.settings.backup_key_file).is_file():
        return None
    if not config.get("backup_requested_at") and not _backup_is_due(config, now):
        return None
    ctx.db.run("UPDATE control_backup_config SET last_backup_status='running',last_backup_detail='',updated_at=now() WHERE id=true")
    try:
        key = backups.read_key(ctx.settings.backup_key_file)
        result = backups.create_backup(ctx.settings.database_url, ctx.settings.control_repo, ctx.settings, config,
                                       key, now)
        ctx.db.run("UPDATE control_backup_config SET last_backup_at=%s,last_backup_name=%s,last_backup_status='success',"
                   "last_backup_detail=%s,backup_requested_at=NULL,updated_at=now() WHERE id=true",
                   (now, result["name"], f"Encrypted backup completed; retention removed {len(result['removed'])} old backup(s)."))
        close_incident(ctx, "backup_failure", "scheduled", "scheduled backup completed")
        return result
    except Exception as exc:  # noqa: BLE001 - never include subprocess or S3 response bodies in logs.
        detail = str(exc)[:500] if isinstance(exc, backups.BackupError) else "backup creation failed"
        ctx.db.run("UPDATE control_backup_config SET last_backup_at=%s,last_backup_status='failed',last_backup_detail=%s,"
                   "backup_requested_at=NULL,updated_at=now() WHERE id=true", (now, detail))
        open_incident(ctx, "backup_failure", "scheduled", "warning", "Command Center backup failed", detail)
        log.warning("scheduled backup failed")
        return {"ok": False, "detail": detail}


def _new_scratch_database(database_url: str) -> tuple[str, str]:
    name = "aiflow4_backup_test_" + secrets.token_hex(6)
    admin_url = make_conninfo(database_url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return name, make_conninfo(database_url, dbname=name)


def restore_test(ctx, source: str, now: datetime | None = None, config: dict | None = None) -> dict:
    """Restore to a temporary database and filesystem tree, then drop them regardless of the result."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    db_name = ""
    admin_url = make_conninfo(ctx.settings.database_url, dbname="postgres")
    try:
        key = backups.read_key(ctx.settings.backup_key_file)
        db_name, scratch_url = _new_scratch_database(ctx.settings.database_url)
        with tempfile.TemporaryDirectory(prefix="aiflow4-weekly-restore-test-") as root:
            base = Path(root)
            roots = {name: base / name for name in ("control", "console", "jira", "github")}
            roots["repository"] = base / "repository"
            result = backups.restore_archive(source, key, scratch_url, ctx.settings,
                                             s3_endpoint=str((config or {}).get("s3_endpoint") or ""),
                                             s3_region=str((config or {}).get("s3_region") or "us-east-1"),
                                             restore_roots=roots, install_backup_key=False, scratch_mode=True)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "components": {"database": {"status": "failed", "detail": "scratch restore failed"},
                                                 "stored_secrets": {"status": "not_run", "detail": "scratch restore failed"},
                                                 "control_repository": {"status": "not_run", "detail": "scratch restore failed"},
                                                 "knowledge": {"status": "not_run", "detail": "scratch restore failed"}},
                  "error": str(exc)[:500] if isinstance(exc, backups.BackupError) else "scratch restore failed"}
    finally:
        if db_name:
            try:
                with psycopg.connect(admin_url, autocommit=True) as admin:
                    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name)))
            except Exception:  # noqa: BLE001
                log.warning("could not remove weekly restore-test database")
    return result


def weekly_restore_test(ctx, now: datetime | None = None) -> dict | None:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    config = _config(ctx)
    if not config:
        return None
    last = _as_datetime(config.get("last_test_at"))
    if last is not None and now - last < timedelta(days=7):
        return None
    result = None
    try:
        listing = backups.list_backups(config)
    except backups.BackupError as exc:
        result = {"ok": False, "components": {}, "error": str(exc)}
        listing = []
    if not listing:
        if result is None:
            return None
        result["components"] = {name: {"status": "failed", "detail": "backup could not be listed"}
                                for name in ("database", "stored_secrets", "control_repository", "knowledge")}
    else:
        result = restore_test(ctx, listing[0]["source"], now, config)
    status = "success" if result.get("ok") else "failed"
    detail = ("Weekly restore test completed on a scratch database." if status == "success" else
              result.get("error") or "One or more components failed the scratch restore.")
    ctx.db.run("UPDATE control_backup_config SET last_test_at=%s,last_test_status=%s,last_test_detail=%s,"
               "last_test_components=%s,updated_at=now() WHERE id=true",
               (now, status, detail[:1000], J(result.get("components") or {})))
    if status == "failed":
        open_incident(ctx, "backup_restore_test", "weekly", "warning", "Weekly backup restore test failed", detail)
    else:
        close_incident(ctx, "backup_restore_test", "weekly", "weekly restore test passed on a scratch copy")
    return result

