from __future__ import annotations

import shutil
from pathlib import Path

from aiflow.v4.control import db as control_db


def test_join_token_migration_upgrades_develop_schema(db, tmp_path, monkeypatch):
    current_migrations = Path(control_db.MIGRATIONS)
    develop_migrations = tmp_path / "develop-migrations"
    develop_migrations.mkdir()
    for migration in current_migrations.glob("*.sql"):
        if migration.name != "006_node_join_tokens.sql":
            shutil.copy2(migration, develop_migrations / migration.name)

    # Apply the schema already present on develop, including its legacy
    # node_join_tokens(node, consumed_at) table.
    with db.pool.connection() as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    monkeypatch.setattr(control_db, "MIGRATIONS", develop_migrations)
    applied = db.migrate()
    assert "003_node_operations" in applied
    assert db.one("SELECT column_name FROM information_schema.columns"
                  " WHERE table_name='node_join_tokens' AND column_name='node'") == {"column_name": "node"}
    assert db.one("SELECT column_name FROM information_schema.columns"
                  " WHERE table_name='node_join_tokens' AND column_name='consumed_at'") == {
                      "column_name": "consumed_at"}
    db.run("INSERT INTO node_join_tokens (token_hash, node, expires_at)"
           " VALUES ('legacy-hash', 'legacy-node', now() + interval '5 minutes')")

    monkeypatch.setattr(control_db, "MIGRATIONS", current_migrations)
    assert db.migrate() == ["006_node_join_tokens"]

    columns = {row["column_name"] for row in db.all(
        "SELECT column_name FROM information_schema.columns WHERE table_name='node_join_tokens'")}
    assert {"token_hash", "node_name", "role", "project", "dynamic", "created_at", "expires_at", "used_at"} <= columns
    assert not ({"node", "consumed_at"} & columns)
    assert db.one("SELECT count(*) AS n FROM node_join_tokens") == {"n": 0}
    node_columns = {row["column_name"] for row in db.all(
        "SELECT column_name FROM information_schema.columns WHERE table_name='nodes'")}
    assert {"projects", "dynamic"} <= node_columns
    index = db.one("SELECT indexdef FROM pg_indexes WHERE tablename='node_join_tokens'"
                   " AND indexname='node_join_tokens_pending_node'")
    assert index and "UNIQUE INDEX" in index["indexdef"] and "WHERE (used_at IS NULL)" in index["indexdef"]
