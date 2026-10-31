from __future__ import annotations

import ast
import json
from pathlib import Path

from fastapi import HTTPException

from aiflow.v4.control import route_deps
from test_backups import as_role


ROOT = Path(__file__).resolve().parents[2]


def test_configure_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db, tmp_path):
    ctx.settings.backup_path = str(tmp_path)
    destination = tmp_path / "scheduled"
    as_role(client, ctx)

    response = client.put("/v1/backups/config", json={"schedule": "weekly", "destination_kind": "local",
                                                       "destination_path": str(destination), "retention_days": 45})

    assert response.status_code == 200
    events = db.all("SELECT type FROM domain_events WHERE type='backup.configured'")
    assert events == [{"type": "backup.configured"}]
    audits = db.all("SELECT target, action, ticket, run_id, fence, actor, accepted, result, payload FROM write_audit "
                    "WHERE target='backup' AND action='configure'")
    assert audits == [{"target": "backup", "action": "configure", "ticket": "", "run_id": "", "fence": None,
                       "actor": "backup@example.test", "accepted": True, "result": "backup settings updated",
                       "payload": {"schedule": "weekly", "destination": "local", "retention_days": 45}}]


def test_run_now_publishes_one_event_and_writes_the_same_audit_row(client, ctx, db):
    as_role(client, ctx)

    response = client.post("/v1/backups/run")

    assert response.status_code == 200
    events = db.all("SELECT type FROM domain_events WHERE type='backup.run_requested'")
    assert events == [{"type": "backup.run_requested"}]
    audits = db.all("SELECT target, action, ticket, run_id, fence, actor, accepted, result, payload FROM write_audit "
                    "WHERE target='backup' AND action='run_now'")
    assert audits == [{"target": "backup", "action": "run_now", "ticket": "", "run_id": "", "fence": None,
                       "actor": "backup@example.test", "accepted": True, "result": "backup queued", "payload": {}}]


def test_key_download_publishes_one_event_and_the_refused_second_call_none(client, ctx, db, tmp_path):
    key_path = tmp_path / "secrets" / "backup.key"
    ctx.settings.backup_key_file = str(key_path)
    as_role(client, ctx)

    first = client.post("/v1/backups/key")

    assert first.status_code == 200
    assert key_path.read_bytes() == first.content
    events = db.all("SELECT type, payload FROM domain_events WHERE type='backup.key_downloaded'")
    assert events == [{"type": "backup.key_downloaded", "payload": {"actor": "backup@example.test", "scope": "control"}}]
    event_payload_bytes = json.dumps([row["payload"] for row in events], sort_keys=True).encode()
    assert first.content not in event_payload_bytes
    assert str(key_path).encode() not in event_payload_bytes
    audits = db.all("SELECT target, action, ticket, run_id, fence, actor, accepted, result, payload FROM write_audit "
                    "WHERE target='backup' AND action='download_key' AND accepted=true")
    assert audits == [{"target": "backup", "action": "download_key", "ticket": "", "run_id": "", "fence": None,
                       "actor": "backup@example.test", "accepted": True,
                       "result": "one-time backup key download", "payload": {}}]

    second = client.post("/v1/backups/key")

    assert second.status_code == 409
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE type='backup.key_downloaded'")["n"] == 1
    assert db.one("SELECT count(*) AS n FROM write_audit WHERE target='backup' AND action='download_key' "
                  "AND accepted=true")["n"] == 1


def test_the_audit_row_rolls_back_with_the_configuration(client, ctx, db, tmp_path, monkeypatch):
    ctx.settings.backup_path = str(tmp_path)
    before = db.one("SELECT schedule FROM control_backup_config WHERE id=true")["schedule"]
    as_role(client, ctx)

    def fail_audit(*args, **kwargs):
        raise HTTPException(500, "forced audit failure")

    monkeypatch.setattr(route_deps, "audit", fail_audit)
    response = client.put("/v1/backups/config", json={"schedule": "weekly", "destination_kind": "local",
                                                       "destination_path": str(tmp_path / "scheduled"),
                                                       "retention_days": 45})

    assert response.status_code == 500
    assert db.one("SELECT schedule FROM control_backup_config WHERE id=true")["schedule"] == before
    assert db.one("SELECT count(*) AS n FROM domain_events WHERE type='backup.configured'")["n"] == 0


def test_the_backups_routes_no_longer_call_audit():
    routes = ROOT / "aiflow/v4/control/features/backups/routes.py"
    module = ast.parse(routes.read_text(encoding="utf-8"))

    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "audit"
                   for node in ast.walk(module))
