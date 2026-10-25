# Machines and AI management

The owner uses **Machines and AI** in the console to enroll machines and manage the AI accounts assigned to them.
The screen reads the live registry, including machine state, heartbeat metrics, processors, active sessions, account
quota, and today's recorded token and cost totals.

## Add a machine

Choose a machine name, role, and project. AI Flow creates the same node-bound, one-use join grant used by the
Command Center. The displayed bootstrap command expires five minutes after issuance; the screen counts down and
keeps polling live state while the node installs and joins. A pending enrollment is not shown as a joined machine.
Removing a machine revokes its node credential and hides its registry record. It is refused while that machine
owns an active run.

## Add and manage AI accounts

Choose a provider, one to eight concurrent sessions, and one or more jobs (develop, verify, intake, or master).
The API enforces a total of at most eight configured sessions per machine. A new account stays in the pending
sign-in section while the assigned node starts that provider's login flow inside the new account's private home.
The console shows the authorization link and code emitted by the provider CLI. The owner opens the link in a
browser, enters the code when asked, then finishes sign-in on screen after the node's account probe succeeds.
Provider credentials stay in the account's home on the machine and are not copied to the control plane. The account
then appears with its live quota and usage. If the handoff fails or expires at the provider, the owner can start a
fresh sign-in from the screen.

Owners can pause or resume an account, request another sign-in handoff, change sessions and jobs, or remove it.
Paused accounts are removed from the node's desired worker set; session and job edits reach the node on its next
heartbeat. Removing an account is refused while it owns an active run. Stopping a machine from taking new work
sets its registry status to draining; its current runs can finish. Removing a machine and stopping new work both
require an explicit confirmation in the screen and API.

Every accepted management operation is written to `write_audit` with its owner, subject and changed values.
Changes use registry state and heartbeat reconciliation; they do not require a service or machine restart.

## Registry API

- `POST /v1/machines/{name}/accounts` adds an account, serializes the eight-session capacity check, and queues
  provider sign-in on the assigned node.
- `GET|POST /v1/accounts/{account_id}/sign-in` reads provider-generated authorization details or starts a new
  provider login.
- `POST /v1/accounts/{account_id}/sign-in/status` lets the assigned node report its provider login handoff and
  completion; only the assigned node token can report it.
- `PATCH /v1/accounts/{account_id}` changes jobs, sessions, or pause state.
- `DELETE /v1/accounts/{account_id}` requires `{ "confirm": true }`.
- `PUT /v1/nodes/{name}/work-acceptance?accepting_work=false` requires `{ "confirm": true }`.
- `DELETE /v1/nodes/{name}` requires `{ "confirm": true }`.

Tests: `tests/v4/test_machines.py` covers the screen, one-use join expiry, account capacity and state, confirmation,
quota and daily usage, immediate worker reconciliation, and audit records.
