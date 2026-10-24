# Machines, AI accounts and worker counts live in the database

**Console screens:** Machines and AI (`machines`)

A machine and the AI accounts on it are runtime facts of an installation, not source code. The owner manages them
on the **Machines and AI** screen ([machines-and-ai.md](machines-and-ai.md)); nothing needs a file edit, a commit
or a restart, and the control repository (`aiflow-control`) is never written.

## What is stored where

| Fact | Where |
| --- | --- |
| Machines, their projects and heartbeat | control database, `nodes` |
| AI accounts, provider, name, roles, workers, pause | control database, `managed_ai_accounts` |
| Total session limit and extra machines of a shared account | `shared_accounts`, `account_machines` |
| Rules, profiles, tool versions, project definitions | control repository (versioned policy) |

`accounts.yaml`, `nodes/*.yaml` and project-level `nodes` assignments are no longer read for accounts, roles,
worker counts or project assignment once the import below ran. Node project assignments and project-level machine
assignments are copied to the database together. A node file may still carry hardware hints (`packages`, `services`,
`tools`); they are setup details, not scheduling facts.

## Join, add, change

- A machine that joins with its one-use token appears on the screen on its own and serves the project it joined
  for (plus any project that lists it under `nodes`). No project file lists machines.
- **Add AI account** takes provider, an optional name, jobs (roles) and sessions (workers). The account id stays
  generated (`<provider>-<machine>-NN`). Sign-in is a provider link and code shown on the screen.
- Sessions and jobs are changed on the same screen. The node reads them on its next heartbeat, so a change is applied
  within the heartbeat interval (well under a minute) with no restart.

## API

- `POST /v1/nodes/join-tokens` and `POST /v1/nodes/{name}/join-command` create the one-use token and the join
  command shown on the screen.
- `POST /v1/machines/{name}/accounts` adds an account (provider, optional name, sessions, jobs) and returns its
  sign-in link and code.
- `GET /v1/state` and `GET /v1/deploy/state` read machines and accounts from the database, not from files.

## One-time import

Older installations keep machines and accounts in `accounts.yaml` and `nodes/*.yaml`. On the first start of the
control API after the upgrade they are copied into the database once, and the import is recorded in
`control_imports`. It never runs again and never overwrites a row, so anything changed or removed on screen stays as
it is. If a configured account could not be synced because of the licence limits, the import waits for a later start.
Afterwards the files are ignored for these facts.

Developers and CI can run it directly: `aiflow4 import-inventory` (a repeat prints "already imported").

## Backup and restore

The backup is a full database dump ([backup-restore.md](backup-restore.md)), so machines, accounts, roles, worker
counts and the import marker are in every backup and come back with a restore; the control repository part of the
backup holds policy only.

Tests: `tests/v4/test_inventory_in_database.py`.
