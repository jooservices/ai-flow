"""Shared memory of the AIs: facts, cases, lessons and context in PostgreSQL, behind a small service that keeps the credential.

    store.py    the SQL (needs the pg8000 driver; only the service imports it)
    guard.py    what may be stored (size, secrets) and how a query becomes a tsquery
    service.py  the HTTP service (roles, namespaces)
    client.py   what the supervisor, the control and `aiflow-mem` use (standard library only; memory is advice, never required)
"""
