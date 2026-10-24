# Runtime secrets and environment settings

Owners and super administrators manage runtime values from **Settings** at `/settings`. The page and its API use
the `console.manage` permission; unauthenticated requests receive 401 and other roles receive 403. Jira credentials
and the GitHub App remain on their existing Integrations screens. Generated control credentials and installation
tokens are not editable here.

**Console screens:** Settings (`settings`)

The console API is `GET /v1/runtime-settings` to list key metadata, `POST /v1/runtime-settings` to set a value and
queue its restart, and `GET /v1/runtime-settings/changes/{change_id}` to read the result. The request body contains
`scope`, `project`, `key`, and `value`; responses contain metadata and service results only. The control-node
protocol uses `POST /v1/nodes/{name}/heartbeat` to advertise a public encryption key and key-name inventory, and
`POST /v1/nodes/{name}/operations/{operation_id}/result` to report an operation outcome. The heartbeat and result
never carry a value in plaintext; the result's encrypted recovery snapshot is omitted from owner responses.

## Files and key inventory

The screen lists key names, whether a value is set, that key's last-change time, and the machines that report a
project key. Each env file stores a timestamp comment per managed key; changing one key leaves the other keys'
timestamps intact. Legacy entries without metadata start with the file's existing modification time. The screen
never reads a value back to the browser. This applies to settings as well as keys recognized as secrets. Names
containing `TOKEN`, `PASSWORD`, `SECRET`, `PRIVATE_KEY`, `API_KEY`, `CREDENTIAL`, or `BASIC` are labelled **Secret**;
other names are labelled **Setting**.

| Screen location | File | Consumer |
| --- | --- | --- |
| Control runtime secret or setting | `/etc/aiflow/secrets/runtime.env` | `control-api-a` and `control-api-b` |
| Project environment | Backed up copy: `/etc/aiflow/projects/<KEY>.env` on the control host; working copies: the same path on each registered machine serving that project | Worker services assigned to those machines |

Control runtime changes for `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN` are refused. These
credentials are consumed by scheduled backups in `control-loops`, which does not load `runtime.env`; the screen
does not claim to update or restart that process. Other non-reserved control runtime keys are applied to both API
containers. Jira, GitHub, installation and generated control credentials keep their existing flows.

The control installer creates the runtime env file and the projects directory with owner-only permissions. A project
change includes the control host's backed up copy in the same coordinated node operation as the worker copies. If
any machine fails, the coordinator rolls back every successful copy before work resumes. The encrypted control
backup contains this canonical copy; when a replacement worker first heartbeats without running workers, the
control API sends its missing project settings encrypted to that node, and the node writes them before starting
assigned workers. The node response never contains these values in plaintext. Changes written to the control
runtime file use single-quoted Compose env-file values, so `$NAME` and `${NAME}` remain literal through a restart.
Control runtime values containing quotes or backslashes are refused because the shared env-file readers do not
interpret those escape sequences. The Jira, GitHub, console-login and generated control secret stores keep their
existing flows and are not included in this screen. The first console-less installation still uses
`aiflow4 secret set NAME`.

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
  stopped worker units read the new file when they next start. The machines remain drained until every apply
  operation passes its health checks. If one machine fails, queued peer changes are cancelled and each successful
  peer receives an encrypted snapshot of its own prior file to restore and health-check before work resumes.
- After a failed write, restart, or health check, the node atomically restores the previous file and checks the
  affected services against that restored value. A node resumes its prior work-acceptance state when its operation
  reports, once the fleet coordinator confirms the whole change succeeded or recovered. If a peer rollback or its
  health check fails, the screen names the node and service and the affected machines remain paused. Operation
  results and the owner screen expose only key names and recovery outcomes; snapshots stay encrypted to their node.

If a project machine is offline or already draining, save is refused and no file changes. If active work does not
drain before the timeout, the node keeps the previous value and reports the failure without restarting a worker.
No shell command is required from the owner.

## Backup and restore

The control backup's stored-secrets component includes `/etc/aiflow/secrets/runtime.env` and the project env files
in `/etc/aiflow/projects` on the control host, in addition to the existing control, Jira, GitHub and console
stores. Settings managed from this screen have a canonical copy there; node agents write encrypted deliveries to
each worker's local project file. Restoring the encrypted backup restores the canonical copy, which replacement
workers receive on their first heartbeat before their workers start. The control API mounts these paths read-only
for inventory; the control node agent writes the canonical project copy as part of the coordinated operation.
