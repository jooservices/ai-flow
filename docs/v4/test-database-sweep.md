# Leftover test databases

Each pytest session creates a database of its own, `aiflow4_t_<epoch>_<hex>`, and drops it at the end. A session
that is killed or times out never drops it, and each one holds about 95 MB: on 21 October the lab's test
PostgreSQL held 117 of them, nearly 8 GB.

- `aiflow/v4/core/testdb.py` names the databases (the creation time is part of the name) and sweeps them.
- The sweeper drops a database that no connection uses and that is older than two hours. A name without a time
  (from before this change) goes as soon as nothing uses it. When more than 8 test databases exist, idle ones
  older than ten minutes go too, oldest first. A database in use or younger than ten minutes is never touched.
- The node agent runs the sweep every ten minutes with the `AIFLOW4_TEST_DATABASE_URL` found in
  `/etc/aiflow/projects/*.env`; the URL is never logged. The role must own the databases or be a superuser; the
  role the lab uses created them, so it does.
- The pytest fixture also sweeps before it creates its database, so a laptop run cleans up after itself.
