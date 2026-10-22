from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.db import DB
from aiflow.v4.core import backups


SESSION_SECRET = "backup-test-session-secret-that-is-long-enough"


def as_role(client, ctx, role="owner"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session("backup@example.test", role, 2_000_000_000,
                                                          SESSION_SECRET))
    return client


def configure_paths(ctx, root: Path, repo: Path | None = None) -> dict[str, Path]:
    roots = {name: root / name for name in ("control", "console", "jira", "github")}
    for path in roots.values():
        path.mkdir(parents=True)
    ctx.settings.control_secrets_dir = str(roots["control"])
    ctx.settings.console_secrets_dir = str(roots["console"])
    ctx.settings.jira_secret_dir = str(roots["jira"])
    ctx.settings.github_app_dir = str(roots["github"])
    ctx.settings.backup_key_file = str(roots["control"] / "backup.key")
    if repo is not None:
        ctx.settings.control_repo = str(repo)
    roots["repository"] = Path(ctx.settings.control_repo)
    return roots


def test_backup_screen_and_owner_settings_api(client, ctx, tmp_path):
    from aiflow.v4.control.ui import mount_ui

    mount_ui(client.app, ctx)
    page = client.get("/backup")
    assert page.status_code == 200
    assert b"Backup &amp; Restore" in page.content
    assert b"Download encryption key once" in page.content
    assert b"backup restore --from" in page.content

    ctx.settings.console_session_secret = SESSION_SECRET
    assert client.get("/v1/backups").status_code == 401
    as_role(client, ctx, "lead")
    assert client.get("/v1/backups").status_code == 403
    as_role(client, ctx)
    initial = client.get("/v1/backups")
    assert initial.status_code == 200
    assert initial.json()["schedule"] == "daily"
    assert initial.json()["destination_kind"] == "local"
    assert initial.json()["retention_days"] == 30

    ctx.settings.backup_path = str(tmp_path)
    destination = tmp_path / "scheduled"
    saved = client.put("/v1/backups/config", json={"schedule": "weekly", "destination_kind": "local",
                                                   "destination_path": str(destination), "retention_days": 45})
    assert saved.status_code == 200, saved.text
    assert saved.json()["schedule"] == "weekly"
    assert saved.json()["destination_path"] == str(destination)
    assert saved.json()["retention_days"] == 45
    assert client.put("/v1/backups/config", json={"destination_kind": "local", "destination_path": "/"}).status_code == 400

    mounted = client.put("/v1/backups/config", json={"destination_kind": "mounted",
                                                       "destination_path": str(tmp_path / "mounted"),
                                                       "retention_days": 45})
    assert mounted.status_code == 200
    assert mounted.json()["destination_kind"] == "mounted"

    ctx.settings.backup_path = str(tmp_path / "local-root")
    local = client.put("/v1/backups/config", json={"destination_kind": "local",
                                                     "destination_path": str(Path(ctx.settings.backup_path) / "nightly"),
                                                     "retention_days": 45})
    assert local.status_code == 200
    assert local.json()["destination_path"].endswith("/local-root/nightly")

    s3 = client.put("/v1/backups/config", json={"destination_kind": "s3", "s3_endpoint": "https://s3.example.test",
                                                 "s3_bucket": "backup-bucket", "s3_prefix": "cc/daily",
                                                 "s3_region": "us-east-2", "retention_days": 14})
    assert s3.status_code == 200, s3.text
    assert s3.json()["destination_kind"] == "s3"
    assert s3.json()["s3_bucket"] == "backup-bucket"


def test_encryption_key_download_is_one_time_and_private(client, ctx, tmp_path):
    ctx.settings.backup_key_file = str(tmp_path / "secrets" / "backup.key")
    as_role(client, ctx)
    first = client.post("/v1/backups/key")
    assert first.status_code == 200
    assert first.headers["content-type"] == "application/octet-stream"
    assert len(first.content) == 32
    assert Path(ctx.settings.backup_key_file).read_bytes() == first.content
    assert os.stat(ctx.settings.backup_key_file).st_mode & 0o777 == 0o600
    assert client.post("/v1/backups/key").status_code == 409
    status = client.get("/v1/backups").json()
    assert status["key_downloaded"] is True
    assert "key" not in status


def _restore_database_snapshot(path: Path, database_url: str) -> None:
    data = json.loads(path.read_text())
    with psycopg.connect(database_url) as conn:
        for row in data["tickets"]:
            conn.execute("INSERT INTO tickets (key,project,status,type,priority,labels,summary,description,parent_key,blocked_by,fields)"
                         " VALUES (%(key)s,%(project)s,%(status)s,%(type)s,%(priority)s,%(labels)s,%(summary)s,%(description)s,%(parent_key)s,%(blocked_by)s,%(fields)s::jsonb)",
                         {**row, "fields": json.dumps(row["fields"])})
        for row in data["managed_resources"]:
            row["updated_at"] = datetime.fromisoformat(row["updated_at"])
            conn.execute("INSERT INTO managed_resources (resource_type,resource_id,title,content,archived,path,version,updated_by,updated_at,commit_sha)"
                         " VALUES (%(resource_type)s,%(resource_id)s,%(title)s,%(content)s,%(archived)s,%(path)s,%(version)s,%(updated_by)s,%(updated_at)s,%(commit_sha)s)", row)
        for row in data["memory"]:
            row["created_at"] = datetime.fromisoformat(row["created_at"])
            conn.execute("INSERT INTO memory (id,project,kind,ref,ticket,text,created_at)"
                         " VALUES (%(id)s,%(project)s,%(kind)s,%(ref)s,%(ticket)s,%(text)s,%(created_at)s)", row)


def test_sample_installation_full_backup_restore_round_trip(ctx, db, test_database_url, control_repo, tmp_path, monkeypatch):
    source_roots = configure_paths(ctx, tmp_path / "original", control_repo)
    ctx.settings.database_url = test_database_url
    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    db.run("INSERT INTO tickets (key,project,status,summary,description) VALUES ('SAMPLE-229','SAMPLE','Done','Original ticket','Original database data')")
    db.run("INSERT INTO managed_resources (resource_type,resource_id,title,content,path,version,updated_by,updated_at)"
           " VALUES ('knowledge','sample-data','Sample knowledge','Original knowledge','knowledge/sample-data.md',1,'sample',%s)",
           (fixed,))
    db.run("INSERT INTO memory (project,kind,ref,ticket,text,created_at) VALUES ('SAMPLE','answer','sample-ref','SAMPLE-229','Original indexed knowledge',%s)",
           (fixed,))
    (control_repo / "knowledge").mkdir(exist_ok=True)
    (control_repo / "knowledge" / "sample-data.md").write_text("repository knowledge content\n")
    (source_roots["control"] / "aiflow4.env").write_text("DATABASE_PASSWORD=encrypted in backup\n")
    (source_roots["jira"] / "jira.env").write_text("JIRA_API_TOKEN=encrypted in backup\n")
    (source_roots["github"] / "app.pem").write_text("github app key\n")
    (source_roots["console"] / "admin-password").write_text("admin secret\n")
    key = secrets.token_bytes(32)
    backups.install_key(ctx.settings.backup_key_file, key)

    def fake_pg(command, label, timeout=3600):
        if label == "database backup":
            source_url = command[-1]
            with psycopg.connect(source_url, row_factory=dict_row) as conn:
                snapshot = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} WHERE " +
                                   ("key='SAMPLE-229'" if table == "tickets" else "true") + " ORDER BY 1").fetchall()]
                            for table in ("tickets", "managed_resources", "memory")}
            path = Path(command[command.index("--file") + 1])
            path.write_text(json.dumps(snapshot, default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value)))
        else:
            dump = Path(command[-1])
            target_url = command[command.index("--dbname") + 1]
            _restore_database_snapshot(dump, target_url)

    monkeypatch.setattr(backups, "_run_db_command", fake_pg)
    destination = tmp_path / "original-backups"
    config = {"destination_kind": "local", "destination_path": str(destination), "retention_days": 30}
    made = backups.create_backup(test_database_url, control_repo, ctx.settings, config, key, fixed)
    archive = destination / made["name"]
    assert archive.is_file()
    assert not archive.read_bytes().startswith(b"SAMPLE")

    target_name = "aiflow4_restore_" + secrets.token_hex(6)
    admin_url = make_conninfo(test_database_url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_name)))
    target_url = make_conninfo(test_database_url, dbname=target_name)
    target_db = DB(target_url)
    target_db.migrate()
    target_db.close()
    restore_base = tmp_path / "restored"
    restore_roots = {name: restore_base / name for name in ("control", "console", "jira", "github")}
    restore_roots["repository"] = restore_base / "repository"
    for path in restore_roots.values():
        path.mkdir(parents=True)
    try:
        outcome = backups.restore_archive(str(archive), key, target_url, ctx.settings, restore_roots=restore_roots)
        assert outcome["ok"] is True, outcome
        assert {name: item["status"] for name, item in outcome["components"].items()} == {
            "database": "restored", "stored_secrets": "restored", "control_repository": "restored", "knowledge": "restored"}
        with psycopg.connect(target_url, row_factory=dict_row) as restored:
            ticket = restored.execute("SELECT summary,description FROM tickets WHERE key='SAMPLE-229'").fetchone()
            managed = restored.execute("SELECT title,content FROM managed_resources WHERE resource_id='sample-data'").fetchone()
            memory = restored.execute("SELECT text FROM memory WHERE ref='sample-ref'").fetchone()
        assert ticket == {"summary": "Original ticket", "description": "Original database data"}
        assert managed == {"title": "Sample knowledge", "content": "Original knowledge"}
        assert memory == {"text": "Original indexed knowledge"}
        assert (restore_roots["control"] / "aiflow4.env").read_text() == "DATABASE_PASSWORD=encrypted in backup\n"
        assert (restore_roots["jira"] / "jira.env").read_text() == "JIRA_API_TOKEN=encrypted in backup\n"
        assert (restore_roots["github"] / "app.pem").read_text() == "github app key\n"
        assert (restore_roots["console"] / "admin-password").read_text() == "admin secret\n"
        assert (restore_roots["repository"] / "knowledge" / "sample-data.md").read_text() == "repository knowledge content\n"
        assert not (restore_roots["control"] / "backup.key").exists()
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(target_name)))


def test_wrong_and_missing_encryption_key_are_rejected(tmp_path):
    key = secrets.token_bytes(32)
    wrong = secrets.token_bytes(32)
    source = tmp_path / "aiflow4-20260101T000000Z-1234abcd.aiflow4"
    plain = tmp_path / "plain"
    plain.write_bytes(b"sample protected payload")
    backups.encrypt_file(plain, source, key)
    with pytest.raises(backups.BackupError, match="could not be read"):
        backups.read_key(tmp_path / "missing-key")
    with pytest.raises(backups.BackupError, match="incorrect"):
        backups.restore_archive(str(source), wrong, "unused", object())


def test_retention_removes_only_expired_backups(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    old = root / "aiflow4-20260101T000000Z-11111111.aiflow4"
    inside = root / "aiflow4-20261001T000000Z-22222222.aiflow4"
    old.write_bytes(b"old")
    inside.write_bytes(b"inside retention")
    (root / "not-a-backup.aiflow4").write_bytes(b"leave")
    removed = backups._retention({"destination_kind": "local", "destination_path": str(root), "retention_days": 30},
                                 datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert removed == [old.name]
    assert not old.exists()
    assert inside.exists()
    assert (root / "not-a-backup.aiflow4").exists()


def test_failing_weekly_restore_test_opens_an_incident(ctx, db, tmp_path, monkeypatch):
    config_path = tmp_path / "scheduled"
    config_path.mkdir()
    name = "aiflow4-20261020T000000Z-11111111.aiflow4"
    (config_path / name).write_bytes(b"fake encrypted backup")
    db.run("UPDATE control_backup_config SET destination_kind='local',destination_path=%s,retention_days=30,last_test_at=NULL WHERE id=true",
           (str(config_path),))
    from aiflow.v4.worker import backups as backup_worker
    monkeypatch.setattr(backup_worker, "restore_test", lambda *args, **kwargs: {
        "ok": False, "error": "database restore failed", "components": {
            "database": {"status": "failed", "detail": "database restore failed"},
            "stored_secrets": {"status": "not_run", "detail": "not restored"},
            "control_repository": {"status": "not_run", "detail": "not restored"},
            "knowledge": {"status": "not_run", "detail": "not restored"}}})
    result = backup_worker.weekly_restore_test(ctx, datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert result["ok"] is False
    incident = db.one("SELECT kind,subject,title,closed_at FROM incidents WHERE kind='backup_restore_test' AND subject='weekly'")
    assert incident and incident["title"] == "Weekly backup restore test failed"
    assert incident["closed_at"] is None
