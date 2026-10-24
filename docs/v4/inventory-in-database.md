# Machines, AI accounts and worker counts live in the database

**Console screens:** Command Center (`cc`) and Machines and AI (`machines`)

A machine and the AI accounts on it are runtime facts of an installation, not source code. The owner manages them
on the **Machines and AI** screen ([machines-and-ai.md](machines-and-ai.md)); screen changes need no file edit,
commit or restart and do not write to the control repository. Command Center shows the machine and account state;
operators change it on Machines and AI.

## What is stored where

| Fact | Where |
| --- | --- |
| Machines, their profiles, projects and heartbeat | control database, `nodes` |
| AI accounts, provider, name, roles, workers, pause | control database, `managed_ai_accounts` |
| Total session limit and extra machines of a shared account | `shared_accounts`, `account_machines` |
| Rules, profiles, tool versions, project definitions | control repository (versioned policy) |

`accounts.yaml`, `nodes/*.yaml` and project-level `nodes` assignments are imported once, then removed from the active
checkout. Node profile fields (`purpose`, `notes`, `location`, capabilities, tools, packages and services) are
preserved in `nodes.profile`; assignments are stored in `nodes.projects`. If a later repository refresh restores an
old file, runtime scheduling ignores it and backups filter it out.

## Join, add, change

- A machine that joins with its one-use token appears on the screen on its own and serves the project it joined
  for. Rejoining preserves its stored project assignments and adds the token's project if needed. Project
  assignments are held in `nodes.projects`; project files do not list machines.
- **Add AI account** takes provider, an optional name, jobs (roles) and sessions (workers). The account id stays
  generated (`<provider>-<machine>-NN`). Sign-in is a provider link and code shown on the screen.
- Sessions and jobs are changed on the same screen. The node reads them on its next heartbeat, so a change is applied
  within the heartbeat interval (well under a minute) with no restart.

## API

- The screen's join command uses `GET /v1/install.sh` to download the node bootstrap with its one-use token.
- `POST /v1/nodes/join-tokens` and `POST /v1/nodes/{name}/join-command` create the one-use token and the join
  command shown on the screen.
- `POST /v1/machines/{name}/accounts` adds an account (provider, optional name, sessions, jobs) and returns its
  sign-in link and code.
- `POST /v1/projects` creates a project: the machines allowed to serve it are saved in the database and the project
  file carries no `nodes` list.
- `PATCH /v1/accounts/{id}` changes sessions, roles (jobs) or pause. An account that runs on several machines
  (for example one imported with legacy extra machines) keeps one total session limit; the Machines and AI
  screen offers its **Jobs** editor, which saves the roles to the database.
- `GET /v1/state` and `GET /v1/deploy/state` read machines and accounts from the database, not from files.

## One-time import

Older installations keep machines and accounts in `accounts.yaml` and `nodes/*.yaml`. On the first start of the
control API after the upgrade they are copied into the database once, and the import is recorded in
`control_imports`. The importer removes those legacy files from the active checkout and commits their removal from
the control repository's current branch, including stripping project-level `nodes` entries. It never imports again
or overwrites a row, so anything changed or removed on screen stays as it is. If a configured account could not be
synced because of licence limits, the import waits for a later start. Plan capacity is managed separately on the
Licensing screen through `PUT /v1/licensing/limits`.

Developers and CI can run it directly: `aiflow4 import-inventory` (a repeat prints "already imported" and retries
checkout cleanup if a refresh restored legacy files; when a GitHub App is configured, it also removes the legacy
files and assignments from the repository's current branch).

## Backup and restore

The backup is a full database dump ([backup-restore.md](backup-restore.md)), so machines, profiles, accounts, roles,
worker counts and the import marker are in every backup and come back with a restore. The control repository backup
component omits `accounts.yaml`, `nodes/` and project-level machine assignments, so it contains policy only even if
a stale checkout has restored legacy files.

Tests: `tests/v4/test_inventory_in_database.py`.
