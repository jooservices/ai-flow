# Test services on every node

A project declares the services its tests need in `aiflow-control/projects/<KEY>.yaml`:

```yaml
key: WEB
repo: jooservices/web
services: [postgres, redis]
```

`postgres` (alias `postgresql`) and `redis` are supported (`aiflow/v4/core/services.py`). Projects without
`services` are unaffected.

## What the node agent prepares

The heartbeat's `desired.project_services` lists the services of the projects the node serves. The agent
(`aiflow/v4/node/services.py`) runs them as Docker containers on the host, like the lab machines did by hand, and
repeats the check every 60 seconds:

| Service | Container | Image | Endpoint | Credentials |
| --- | --- | --- | --- | --- |
| PostgreSQL | `aiflow4-test-pg` | `pgvector/pgvector:0.8.7-pg16` | `127.0.0.1:15499` | user `aiflow`, password `test`, database `aiflow4` |
| Redis | `aiflow4-test-redis` | `redis:7-alpine` | `127.0.0.1:16399` | none |

- A missing container is created; a stopped one is started, or removed and recreated when it will not start.
- Containers use `--restart unless-stopped` (they return after a machine restart; the agent recreates them if not)
  and a small footprint: 512 MB / 128 MB memory caps, PostgreSQL with `fsync=off` and 64 MB shared buffers, Redis
  without persistence.
- PostgreSQL gets `CREATE EXTENSION vector` in `aiflow4` and in `template1`, so each pytest session's own database
  (created from `template1`, or from `AIFLOW4_TEST_DATABASE_TEMPLATE`) carries pgvector and is dropped at the end as
  before. Redis keys stay prefixed per test.
- The health check is `pg_isready` plus the extension for PostgreSQL and `PING` for Redis.

## Reporting and admission

Each heartbeat carries `services`: per service `state` (`running`, `started`, `recreated`, `unhealthy`), `healthy`,
`detail`, `endpoint`, `image`; the control API keeps it in the node's `metrics.services`.

A service that is declared but unhealthy, or not reported yet, keeps that node from taking work for the project
(`/v1/work/candidates`); other projects on the node stay available. The watchdog opens a `node_services` warning
incident per node and project naming the service and the reason, and closes it when a later heartbeat reports the
service healthy. The idle-flow incident lists the same cause.

## Command Center

The node detail view has a **Test services** section: each service with its endpoint, state, reason when unhealthy and
the projects that declared it.
