# Administrator guide: backup

**Assumes:** you are a Super Administrator with sudo on the control machine, AI Flow v4 is installed with the
standard layout (`/srv/aiflow/control`), and you have a backup location outside this machine.

## What to save

| What | Where | Why |
|---|---|---|
| The registry (tickets, runs, incidents, users) | PostgreSQL database in container `aiflow4-postgres`; files under `/srv/aiflow/control/data/postgres` | The system of record for AI Flow |
| Memory and knowledge | Same PostgreSQL database | Study cases, skills, policies and their history |
| Logs and metrics | `/srv/aiflow/control/data/openobserve` | Optional; history for investigations |
| Secrets and settings | `/etc/aiflow` | Needed to start again. Copy the directory as a whole into an encrypted store; never print, paste or email its files |
| The control repository | `aiflow-control` (a Git repository) | Accounts, nodes, projects, `versions.lock`. Git already keeps its history if you push it to a remote |

Redis holds leases and short-lived state. It does not need a backup: after a restart, leases expire and runs go back to To Do.

## How

1. Pick a quiet moment. A consistent database dump is enough; you do not need to stop AI Flow.
2. Dump the database from the container to a file on the machine and move the file off the machine. The container name is `aiflow4-postgres`; use `pg_dump` with the database user from the container's environment.
3. Copy `/etc/aiflow` to the encrypted store.
4. Keep at least one backup older than the last upgrade.

## Restore

1. Install the same release on a new machine ([install](install.md)).
2. Put the secrets back in `/etc/aiflow` before the first start, then restore the database dump into PostgreSQL.
3. Start the control plane and check [Command Center](../screens/cc.md). Open runs have lost their leases and return to To Do by themselves.

Test a restore on a spare machine at least once a quarter.
