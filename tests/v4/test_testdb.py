"""Leftover per-session test databases are swept; databases in use or still young stay."""
from __future__ import annotations

import time
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from aiflow.v4.core import testdb
from aiflow.v4.node import agent
from tests.v4.conftest import DATABASE_URL


@pytest.fixture
def admin():
    conn = psycopg.connect(make_conninfo(DATABASE_URL, dbname="postgres"), autocommit=True)
    made: list[str] = []

    def make(name: str) -> str:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        made.append(name)
        return name

    conn.make = make  # type: ignore[attr-defined]
    yield conn
    for name in made:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    conn.close()


def exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone() is not None


def test_names_carry_their_creation_time():
    now = 1_790_000_000
    name = testdb.new_name(now)
    assert name.startswith("aiflow4_t_1790000000_") and testdb.created_at(name) == now
    assert testdb.created_at("aiflow4_t_0123456789ab") is None  # legacy name
    assert testdb.created_at("aiflow4_template") is None


def test_sweep_drops_old_and_legacy_idle_databases_but_keeps_young_and_used_ones(admin):
    now = int(time.time())
    old = admin.make(testdb.new_name(now - 3 * 3600))
    young = admin.make(testdb.new_name(now - 60))
    legacy = admin.make("aiflow4_t_" + uuid.uuid4().hex[:12])
    used = admin.make(testdb.new_name(now - 4 * 3600))
    other = admin.make("aiflow4_not_a_test_db_" + uuid.uuid4().hex[:8])
    with psycopg.connect(make_conninfo(DATABASE_URL, dbname=used)):  # a live session keeps its database
        dropped = testdb.sweep(DATABASE_URL, now=now)
        assert old in dropped and legacy in dropped
        assert not exists(admin, old) and not exists(admin, legacy)
        assert exists(admin, young) and exists(admin, used) and exists(admin, other)


def test_sweep_trims_idle_databases_beyond_the_limit_but_protects_the_active_session_db(
    admin, test_database_url
):
    now = int(time.time()) + 3600  # make the session database old enough to be a crowding candidate
    fresh = admin.make(testdb.new_name(now - 30))
    ages = [now - 700 - i for i in range(4)]
    mine = [admin.make(testdb.new_name(a)) for a in ages]
    session_db = conninfo_to_dict(test_database_url)["dbname"]
    testdb.sweep(DATABASE_URL, now=now, max_databases=1, protected_databases={session_db})
    assert exists(admin, fresh)  # younger than ten minutes: never trimmed for crowding
    assert not any(exists(admin, n) for n in mine)
    assert exists(admin, session_db)


def test_node_agent_sweeps_the_databases_named_in_the_project_env_files(admin, tmp_path, monkeypatch):
    old = admin.make(testdb.new_name(int(time.time()) - 5 * 3600))
    (tmp_path / "TESTFLOW.env").write_text(f"OTHER=1\nAIFLOW4_TEST_DATABASE_URL='{DATABASE_URL}'\n")
    (tmp_path / "TESTWP.env").write_text("WP_TESTS_DB_HOST=x\n")
    assert agent._test_database_urls(str(tmp_path)) == [DATABASE_URL]
    monkeypatch.setattr(agent, "_last_testdb_sweep", 0.0)
    assert agent.sweep_test_databases(str(tmp_path), every_s=600) >= 1
    assert not exists(admin, old)
    assert agent.sweep_test_databases(str(tmp_path), every_s=600) == 0  # not again within ten minutes


def test_sweeper_keeps_the_active_pytest_session_database(test_database_url, admin):
    name = psycopg.conninfo.conninfo_to_dict(test_database_url)["dbname"]

    dropped = testdb.sweep(DATABASE_URL, now=time.time() + testdb.STALE_AFTER_S + 1, max_databases=0)

    assert name not in dropped
    assert exists(admin, name)
