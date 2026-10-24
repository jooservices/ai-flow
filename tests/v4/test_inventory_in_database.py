"""Machines, AI accounts, roles and worker counts live in the control database (docs/v4/inventory-in-database.md)."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from conftest import join_node
from test_backups import configure_paths
from test_machines import owner_session
from aiflow.v4.control import inventory
from aiflow.v4.control.db import DB
from aiflow.v4.core import backups
from aiflow.v4.core.controlrepo import ControlRepo, Project

INVENTORY_TABLES = ("nodes", "managed_ai_accounts", "shared_accounts", "account_machines", "control_imports")


def heartbeat(client, node: str, token: str) -> dict:
    response = client.post(f"/v1/nodes/{node}/heartbeat", json={"slots_total": 8},
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()["desired"]


def test_machine_joins_without_a_project_file_entry(client, ctx):
    owner_session(client, ctx)
    assert "fresh-box" not in ctx.repo().nodes  # no nodes/fresh-box.yaml, no project lists it
    assert not any("fresh-box" in project.nodes for project in ctx.repo().projects.values())

    joined = join_node(client, "fresh-box")
    assert joined.status_code == 200, joined.text

    state = client.get("/v1/state").json()
    machine = next(node for node in state["nodes"] if node["name"] == "fresh-box")
    assert machine["has_agent"] is True
    row = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name='fresh-box'")
    assert row["dynamic"] is True and row["projects"] == ["TESTWP"]
    assert heartbeat(client, "fresh-box", joined.json()["node_token"])["projects"] == ["TESTWP"]


def test_account_is_added_on_screen_with_provider_name_roles_workers_and_sign_in(client, ctx):
    owner_session(client, ctx)
    join_node(client, "acct-box")

    added = client.post("/v1/machines/acct-box/accounts", json={
        "provider": "claude", "name": "Team plan", "sessions": 2, "jobs": ["develop", "verify"]})
    assert added.status_code == 200, added.text
    account_id = added.json()["account"]
    assert added.json()["name"] == "Team plan"

    row = ctx.db.one("SELECT name,provider,node,roles,parallel FROM managed_ai_accounts WHERE id=%s", (account_id,))
    assert row == {"name": "Team plan", "provider": "claude", "node": "acct-box",
                   "roles": ["dev", "verify"], "parallel": 2}
    shown = next(a for a in client.get("/v1/state").json()["accounts"] if a["id"] == account_id)
    assert shown["name"] == "Team plan" and shown["jobs"] == ["develop", "verify"] and shown["parallel"] == 2
    # Sign-in is done from the screen: the node reports the provider's link and code, the console shows them.
    token = ctx.db.one("SELECT sign_in_request_id FROM managed_ai_accounts WHERE id=%s", (account_id,))
    assert token["sign_in_request_id"]
    assert client.get(f"/v1/accounts/{account_id}/sign-in").json()["status"] == "queued"
    assert ctx.repo().account(account_id).roles == ["dev", "verify"]


def test_workers_and_roles_change_on_screen_and_reach_the_node_on_its_next_heartbeat(client, ctx):
    owner_session(client, ctx)
    joined = join_node(client, "workers-box")
    token = joined.json()["node_token"]
    account_id = client.post("/v1/machines/workers-box/accounts", json={
        "provider": "codex", "sessions": 1, "jobs": ["develop"]}).json()["account"]
    ctx.db.run("UPDATE managed_ai_accounts SET sign_in_status='completed' WHERE id=%s", (account_id,))
    assert heartbeat(client, "workers-box", token)["workers"] == [account_id]

    changed = client.patch(f"/v1/accounts/{account_id}", json={"sessions": 3, "jobs": ["develop", "verify"]})
    assert changed.status_code == 200, changed.text

    desired = heartbeat(client, "workers-box", token)  # the heartbeat is the reconciliation: no restart, no file
    assert desired["workers"] == [account_id, f"{account_id}.2", f"{account_id}.3"]
    assert ctx.repo().account(account_id).roles == ["dev", "verify"]
    assert ctx.db.one("SELECT parallel,roles FROM managed_ai_accounts WHERE id=%s", (account_id,)) == {
        "parallel": 3, "roles": ["dev", "verify"]}


def test_repo_inventory_is_imported_once_and_never_overwrites_screen_changes(client, ctx, control_repo):
    owner_session(client, ctx)
    token = join_node(client, "node1").json()["node_token"]
    assert ctx.db.one("SELECT 1 AS x FROM control_imports") is None

    first = client.app.state.import_inventory()
    assert first == {"imported": True, "machines": 2, "accounts": 3}
    assert ctx.db.one("SELECT parallel,roles FROM managed_ai_accounts WHERE id='codex-01'") == {
        "parallel": 1, "roles": ["dev", "verify", "master"]}

    # After the import the screen is the only place that changes accounts.
    assert client.patch("/v1/accounts/codex-01", json={"sessions": 4, "jobs": ["develop"]}).status_code == 200
    (control_repo / "accounts.yaml").write_text(
        "accounts:\n  - {id: codex-01, provider: codex, node: node1, parallel: 7, roles: [master]}\n"
        "  - {id: ghost-01, provider: codex, node: node1, roles: [dev]}\n")
    assert client.app.state.import_inventory() == {"imported": False, "accounts": 0, "machines": 0}
    assert inventory.import_control_repo(ctx.db, ControlRepo.load(control_repo))["imported"] is False

    # The changed repo files (a new account, other workers) are ignored; the database wins.
    ctx._configured_repo = lambda: ControlRepo.load(control_repo)
    repo = ctx.repo()
    assert repo.account("ghost-01") is None
    assert repo.account("codex-01").parallel == 4 and repo.account("codex-01").roles == ["dev"]
    assert repo.inventory_in_db is True
    desired = heartbeat(client, "node1", token)
    assert [w for w in desired["workers"] if w.startswith("codex-01")] == [
        "codex-01", "codex-01.2", "codex-01.3", "codex-01.4"]
    assert "ghost-01" not in desired["accounts"]


def test_joined_machine_serves_its_project_after_the_import_without_the_node_file(client, ctx, control_repo):
    owner_session(client, ctx)
    client.app.state.import_inventory()
    (control_repo / "nodes" / "node1.yaml").unlink()
    ctx._configured_repo = lambda: ControlRepo.load(control_repo)
    joined = join_node(client, "node1")
    assert joined.status_code == 200, joined.text
    assert heartbeat(client, "node1", joined.json()["node_token"])["projects"] == ["TESTWP"]


def test_project_node_assignments_are_imported_and_then_read_from_database(client, ctx):
    owner_session(client, ctx)
    configured = ctx.repo()
    configured.projects["TESTFLOW"] = Project(key="TESTFLOW", repo="jooservices/testflow", nodes=["node1"])
    assert "TESTFLOW" not in configured.nodes["node1"].projects

    result = client.app.state.import_inventory()
    assert result["imported"] is True
    assert ctx.db.one("SELECT projects FROM nodes WHERE name='node1'")["projects"] == ["TESTWP", "TESTFLOW"]

    # The repo can change after import. Its old assignment cannot add or remove runtime access.
    configured.projects["TESTFLOW"].nodes = ["cc"]
    runtime = ctx.repo()
    node1 = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name='node1'")
    cc = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name='cc'")
    assert runtime.projects_for_node("node1", node1["projects"], node1["dynamic"]) == ["TESTWP", "TESTFLOW"]
    assert runtime.projects_for_node("cc", cc["projects"], cc["dynamic"]) == ["TESTWP"]


def test_backup_and_restore_keep_machines_accounts_roles_and_worker_counts(
        client, ctx, db, test_database_url, control_repo, tmp_path, monkeypatch):
    owner_session(client, ctx)
    join_node(client, "backup-box")
    account_id = client.post("/v1/machines/backup-box/accounts", json={
        "provider": "codex", "name": "Backed up", "sessions": 3, "jobs": ["develop", "master"]}).json()["account"]
    client.app.state.import_inventory()
    source_roots = configure_paths(ctx, tmp_path / "original", control_repo)
    ctx.settings.database_url = test_database_url
    key = secrets.token_bytes(32)
    backups.install_key(ctx.settings.backup_key_file, key)
    (source_roots["control"] / "aiflow4.env").write_text(f"AIFLOW4_DATABASE_URL={test_database_url}\n")
    commands: list[list[str]] = []

    def fake_pg(command, label, timeout=3600, env=None):
        # pg_dump/pg_restore are not run here; the dump is the whole database, so check no table is left out of it.
        if label == "database backup":
            commands.append(command)
            with psycopg.connect(test_database_url, row_factory=dict_row) as conn:
                dump = {table: conn.execute(f"SELECT coalesce(jsonb_agg(to_jsonb(t)),'[]'::jsonb) AS rows FROM {table} t"
                                            ).fetchone()["rows"] for table in INVENTORY_TABLES}
            Path(command[command.index("--file") + 1]).write_text(json.dumps(dump))
        else:
            dump = json.loads(Path(command[-1]).read_text())
            with psycopg.connect(target_url) as conn:
                for table, rows in dump.items():
                    conn.execute(sql.SQL("INSERT INTO {t} SELECT * FROM jsonb_populate_recordset(null::{t}, %s::jsonb)")
                                 .format(t=sql.Identifier(table)), (json.dumps(rows),))

    monkeypatch.setattr(backups, "_run_db_command", fake_pg)
    destination = tmp_path / "backups"
    config = {"destination_kind": "local", "destination_path": str(destination), "retention_days": 30}
    made = backups.create_backup(test_database_url, control_repo, ctx.settings, config, key,
                                 datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert not any(part.startswith(("--table", "--exclude", "-t")) for part in commands[0])
    # What changes after the backup is not in it.
    client.patch(f"/v1/accounts/{account_id}", json={"sessions": 1, "jobs": ["verify"]})

    target_name = "aiflow4_inventory_restore_" + secrets.token_hex(6)
    admin_url = make_conninfo(test_database_url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_name)))
    target_url = make_conninfo(test_database_url, dbname=target_name)
    target_db = DB(target_url)
    target_db.migrate()
    target_db.close()
    roots = {name: tmp_path / "restored" / name for name in ("control", "console", "jira", "github", "repository")}
    for path in roots.values():
        path.mkdir(parents=True)
    try:
        outcome = backups.restore_archive(str(destination / made["name"]), key, target_url, ctx.settings,
                                          restore_roots=roots)
        assert outcome["components"]["database"]["status"] == "restored", outcome
        with psycopg.connect(target_url, row_factory=dict_row) as restored:
            account = restored.execute("SELECT name,provider,node,roles,parallel FROM managed_ai_accounts WHERE id=%s",
                                       (account_id,)).fetchone()
            machines = {r["name"] for r in restored.execute("SELECT name FROM nodes").fetchall()}
            imported = restored.execute("SELECT 1 AS x FROM control_imports WHERE name='inventory'").fetchone()
            repo_account = restored.execute("SELECT parallel FROM managed_ai_accounts WHERE id='codex-01'").fetchone()
        assert account == {"name": "Backed up", "provider": "codex", "node": "backup-box",
                           "roles": ["dev", "master"], "parallel": 3}
        assert {"backup-box", "cc", "node1"} <= machines
        assert imported and repo_account == {"parallel": 1}  # the import marker travels too: it never runs again
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(target_name)))
