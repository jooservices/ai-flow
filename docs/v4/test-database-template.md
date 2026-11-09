# Test database from one template

The tests no longer drop and re-create a schema and run every migration for each test. A pytest session does the
expensive work once and every test then takes a copy.

1. `migrated_template` (session fixture in `tests/v4/conftest.py`) creates one database with
   `testdb.create_from_template` and runs `DB.migrate()` on it. It starts from `AIFLOW4_TEST_DATABASE_TEMPLATE`
   when that is set (a database that already carries pgvector), otherwise from an empty database. It is dropped at the
   end of the session.
2. `db` (per test) copies the migrated template into a database of its own (`aiflow4_t_<epoch>_<hex>`) with
   `CREATE DATABASE ... TEMPLATE`, hands out a `DB` on it, and drops it with `testdb.drop` when the test ends.

Isolation is the same as before or stronger: a test never shares a database with another test, so a row, a role
setting or a sequence value written by one test cannot reach the next. `tests/v4/test_template_database.py` pins
this (a row written in one test is absent in the next, the template stays untouched by its copies).

- `AIFLOW4_TEST_DATABASE_TEMPLATE` must be copyable by the test role: owned by that role, or marked
  `IS_TEMPLATE true`. The test role has `CREATEDB` but is not a superuser, so a template owned by an administrator and
  not marked as a template makes the `db` fixture fail during session setup.
- PostgreSQL refuses to copy a database that has a connection, and two copies of one template cannot run at the same
  moment. `create_from_template` retries such a refusal for up to ten seconds.
- The copies are named like the other test databases, so a killed run is cleaned up by the sweeper in
  [test-database-sweep.md](test-database-sweep.md).
- The test role needs `CREATEDB`, as it already did for the sweeper tests.
- To add a step that every test database needs (seed rows, an extension), do it in `migrated_template` after
  `migrate()`; do not do it in `db`, which would repeat it for every test.
- The copy itself takes about a tenth of a second, but PostgreSQL needs about a second to drop a database on a slow disk
  (it syncs the files). `testdb.BackgroundDropper` therefore drops the copies on one background thread and the session
  waits for it once at the end, so a test never waits for its own drop.
