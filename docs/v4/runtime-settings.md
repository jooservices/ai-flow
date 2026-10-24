# Runtime secrets and environment settings

Owners and super administrators manage runtime values from **Settings** at `/settings`. The page and its API use
the `console.manage` permission; unauthenticated requests receive 401 and other roles receive 403. Jira credentials
and the GitHub App remain on their existing Integrations screens. Generated control credentials and installation
tokens are not editable here.

**Console screens:** Settings (`settings`)

The console API is `GET /v1/runtime-settings` to list key metadata, `POST /v1/runtime-settings` to set a value and
queue its restart, and `GET /v1/runtime-settings/changes/{change_id}` to read the result. The request body contains
`scope`, `project`, `key`, and `value`; responses contain metadata and service results only.

## Files and key inventory

The screen lists key names, whether a value is set, the file's last-change time, and the machines that report a
project key. It never reads a value back to the browser. This applies to settings as well as keys recognized as
secrets. Names containing `TOKEN`, `PASSWORD`, `SECRET`, `PRIVATE_KEY`, `API_KEY`, `CREDENTIAL`, or `BASIC` are
labelled **Secret**; other names are labelled **Setting**.

| Screen location | File | Consumer |
| --- | --- | --- |
| Control runtime secret or setting | `/etc/aiflow/secrets/runtime.env` | `control-api-a` and `control-api-b` |
| Project environment | `/etc/aiflow/projects/<KEY>.env` on each registered machine serving that project | Worker services assigned to those machines |

The control installer creates the runtime env file and the projects directory with owner-only permissions. The
Jira, GitHub, console-login and generated control secret stores keep their existing flows and are not included in
this screen. The first console-less installation still uses `aiflow4 secret set NAME`.

Key names use uppercase environment-variable syntax. Values are non-empty printable ASCII without spaces or line
breaks so the existing env-file reader and atomic writer preserve them as one value. The write API, page, audit
payload and operation status contain only the key name and set state. For node delivery, the control API encrypts
each value to that node's heartbeat-advertised public key; the node decrypts it locally, and the completed
operation record drops the encrypted handoff. Logs contain operation ids and service outcomes only.

## Affected services and safe restart

The page shows the affected services before saving and submits a **Save and restart safely** action.

- A control runtime change recreates `control-api-a` and then `control-api-b` through the installed Compose file.
  The first API must pass its container health check before the second is recreated. At least one API remains
  available during the rolling restart.
- A project change is queued to every registered machine currently serving the project. Each must be online,
  accepting work, and reporting its node encryption key before the change is accepted. The control API stops new
  assignments to those machines, then each node waits for its affected workers to leave `working` or `probing`
  state and remain idle for five seconds. The wait is bounded at 65 minutes. Worker services restart one at a time;
  stopped worker units read the new file when they next start.
- After a failed write, restart, or health check, the node atomically restores the previous file and checks the
  affected services against that restored value. A node resumes its prior work-acceptance state when its operation
  reports. The Settings page polls each operation and names the node and service that failed.

If a project machine is offline or already draining, save is refused and no file changes. If active work does not
drain before the timeout, the node keeps the previous value and reports the failure without restarting a worker.
No shell command is required from the owner.

## Backup and restore

The control backup's stored-secrets component includes `/etc/aiflow/secrets/runtime.env` and the project env files
visible in `/etc/aiflow/projects` on the control host, in addition to the existing control, Jira, GitHub and
console stores. It retains the existing encrypted bundle and restrictive restore permissions. The control API
mounts these paths read-only for inventory; node agents write their own project files locally.
