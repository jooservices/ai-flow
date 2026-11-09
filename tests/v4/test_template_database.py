"""The test database comes from one migrated template; every test still gets a database of its own (JAIF-593)."""
from __future__ import annotations

import pytest
import psycopg
from psycopg.conninfo import conninfo_to_dict

from aiflow.v4.control.db import MIGRATIONS
from aiflow.v4.core import testdb
from tests.v4.conftest import DATABASE_URL


def test_a_test_sees_every_shipped_migration_applied(db):
    applied = {row["version"] for row in db.all("SELECT version FROM schema_migrations")}
    assert applied == {path.stem for path in MIGRATIONS.glob("*.sql")}


def test_a_row_written_by_one_test_is_invisible_to_the_next_one_first(db):
    db.run("INSERT INTO schema_migrations (version) VALUES ('leak-marker')")
    assert db.one("SELECT 1 AS found FROM schema_migrations WHERE version='leak-marker'")


def test_a_row_written_by_one_test_is_invisible_to_the_next_one_second(db):
    assert db.one("SELECT 1 AS found FROM schema_migrations WHERE version='leak-marker'") is None


def test_two_databases_of_one_test_do_not_share_rows(db, copy_of_template):
    db.run("INSERT INTO schema_migrations (version) VALUES ('only-in-db')")
    with psycopg.connect(copy_of_template) as other:
        assert other.execute("SELECT 1 FROM schema_migrations WHERE version='only-in-db'").fetchone() is None


@pytest.fixture
def copy_of_template(migrated_template):
    name = testdb.create_from_template(DATABASE_URL, migrated_template)
    yield testdb.url_for(DATABASE_URL, name)
    testdb.drop(DATABASE_URL, name)


def test_the_copy_of_the_template_starts_migrated_and_leaves_the_template_untouched(migrated_template, copy_of_template):
    with psycopg.connect(copy_of_template, autocommit=True) as copy:
        count = copy.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        copy.execute("INSERT INTO schema_migrations (version) VALUES ('copy-only')")
    assert count == len(list(MIGRATIONS.glob("*.sql")))
    with psycopg.connect(testdb.url_for(DATABASE_URL, migrated_template)) as template:
        assert template.execute("SELECT 1 FROM schema_migrations WHERE version='copy-only'").fetchone() is None


def test_the_db_fixture_uses_a_database_of_its_own_not_the_supplied_one(db):
    name = conninfo_to_dict(db.url)["dbname"]
    assert testdb.created_at(name) is not None
    assert name != conninfo_to_dict(DATABASE_URL)["dbname"]


def test_a_dropped_copy_is_gone():
    name = testdb.new_name()
    testdb.create_from_template(DATABASE_URL, None, name)
    testdb.drop(DATABASE_URL, name)
    with psycopg.connect(testdb.url_for(DATABASE_URL, "postgres")) as admin:
        assert admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone() is None


def test_url_for_keeps_the_credentials_and_changes_only_the_database():
    url = testdb.url_for("postgresql://aiflow4_test:secret@db.example:5432/base", "aiflow4_t_1790000000_abcdef01")
    parts = conninfo_to_dict(url)
    assert (parts["user"], parts["host"], parts["dbname"]) == ("aiflow4_test", "db.example", "aiflow4_t_1790000000_abcdef01")


def test_the_background_dropper_removes_every_submitted_database_by_the_time_it_has_waited():
    names = [testdb.create_from_template(DATABASE_URL, None) for _ in range(2)]
    dropper = testdb.BackgroundDropper(DATABASE_URL)
    for name in names:
        dropper.submit(name)
    dropper.wait()
    with psycopg.connect(testdb.url_for(DATABASE_URL, "postgres")) as admin:
        left = admin.execute("SELECT datname FROM pg_database WHERE datname = ANY(%s)", (names,)).fetchall()
    assert left == []


def test_a_copy_of_a_template_that_stays_in_use_gives_up_after_the_wait():
    template = testdb.create_from_template(DATABASE_URL, None)
    try:
        with (psycopg.connect(testdb.url_for(DATABASE_URL, template)),
              pytest.raises(psycopg.errors.ObjectInUse)):
            testdb.create_from_template(DATABASE_URL, template, wait_s=0.2)
    finally:
        testdb.drop(DATABASE_URL, template)
