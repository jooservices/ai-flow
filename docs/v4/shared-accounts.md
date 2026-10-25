# Shared AI accounts

An AI account exists apart from machines. The owner enables it on several machines in Command Center; the flow counts
its sessions and quota once, across all of them, and never runs more sessions than the account's total limit.

## Use it (Command Center)

1. **Accounts → New shared account**: give an id (`claude-02`), the provider and the total number of concurrent
   sessions. The account now exists without any machine.
2. Open the account and choose **Enable on** a machine. The machine shows *Sign-in needed*.
3. The node agent starts the provider's login command in that machine's private account home and reports the
   provider's sign-in **link and code** to Command Center. Open the link and enter the code. The provider CLI stores
   the credential on that machine. When the login command exits successfully, the agent reports completion and
   releases the assignment so the machine's worker can start its usual credential check. An auth failure returns
   only that machine to *Sign-in needed*. Each machine signs in once, and the link and code are cleared on completion.
4. **Total sessions** changes the limit at any time. Accounts defined in `aiflow-control/accounts.yaml` can be shared
   the same way; their first machine stays defined in the repo and keeps its previous capacity as the limit until you
   change it.
5. **Remove** takes the account off one machine only; other machines keep using it. Removing the last machine, or
   **Remove from all machines**, asks for an on-screen confirmation. For an account defined in
   `aiflow-control/accounts.yaml`, this detaches its machine assignments while leaving the source definition in the
   repo so it can be enabled again later.

The account list shows the account once (quota, status, prediction, `used/total sessions`). The account panel shows a
breakdown per machine below: online or offline, sessions now, sessions in the last 24 hours, sign-in state.

## How the limit and the spread work

- Sessions are the running runs of the account id, whichever machine runs them. Every claim, including a version
  release, checks the account total, machine share and free worker slots while holding account and machine database
  locks. Concurrent workers cannot exceed the account limit or overfill a machine (`409`, ticket lease released).
- Each online, signed-in machine's share is weighted by its worker capacity remaining after other accounts' active
  runs. The total limit is apportioned in whole sessions with the largest-remainder method, so shares sum to at most
  the account limit and follow machine capacity weights (for example, capacities 4:1 with a limit of 4 allocate 3:1).
  A machine with no free worker slot cannot claim work. Offline machines (no heartbeat for 75 s) and machines at
  capacity stop holding up work; the other machines can take the remaining sessions.
- Every machine can run as many worker instances as the limit (`desired.workers`), so any machine can carry the whole
  account when the others are away. The worker checks the local credentials; only a passing check makes that machine
  eligible to claim work.
- Cooldowns, quota and the credential-expiry warning belong to the account; the earliest expiry among machines is used.
- The limit is enforced when the account has an explicit limit (created in Command Center, or shared on a second
  machine). An account only in `accounts.yaml` keeps its old behaviour: `parallel` workers on its one machine.

## Credentials

Command Center never stores credentials. Credentials stay in `/srv/aiflow/accounts/<id>` on each machine that signed
in. Only the sign-in prompt the machine reports (`POST /v1/accounts/<id>/signin-prompt`, https link and a short device
code, kept for at most one hour) and the signed-in state are stored. Removing an account from a machine stops its
workers there; the owner signs out of the provider on the machine to delete the local credential. Sign-in links and
codes are visible only to console users with `nodes.manage`; read-only leads can see account usage and machine state.

## API (developers and CI)

| Call | Purpose |
|---|---|
| `GET /v1/shared-accounts` | accounts, limits, machines |
| `POST /v1/shared-accounts` | create (`id`, `provider`, `tier`, `roles`, `session_limit`) |
| `PUT /v1/shared-accounts/{id}/session-limit` | change the total limit |
| `POST /v1/shared-accounts/{id}/machines` | enable on a machine (`node`) |
| `DELETE /v1/shared-accounts/{id}/machines/{node}[?confirm=true]` | remove from one machine; `confirm` is required for the last one |
| `DELETE /v1/shared-accounts/{id}?confirm=true` | remove from all machines |
| `POST /v1/accounts/{id}/signin-prompt` | node reports link and code (node token) |
| `POST /v1/accounts/{id}/signin-complete` | node reports successful provider login for the current attempt; its worker then checks credentials (node token) |

### Other calls that read or enforce the shared account

| Call | What changes with shared accounts |
|---|---|
| `POST /v1/shared-accounts/{account_id}/machines/{node}/signin` | Retry sign-in on one machine: starts a new attempt with a fresh link and code (Command Center: **Retry sign-in**). |
| `POST /v1/work/claim` | Claim checks the account total, the machine share and free worker slots under the account lock. |
| `POST /v1/version-releases/claim` | A version release claim obeys the same account and machine limits. |
| `GET /v1/version-releases/candidates` | Candidates go to a signed-in peer machine with free capacity when the source account is at quota risk. |

The shared account readiness checks also apply to `GET /v1/master/candidate`, `GET /v1/version-releases/candidates`
and `POST /v1/version-releases/claim`, so paused or full accounts do not appear ready for these roles.
| `GET /v1/master/candidate` | An account counts as ready when any assigned, signed-in, online machine has it. |
| `POST /v1/accounts/{account_id}/probe` | A probe result is recorded per machine: an auth failure returns only that machine to *Sign-in needed*; quota cooldown stays account-wide. |
| `GET /v1/accounts/{account_id}/spec` | The node reads the account spec for its own assignment. |
| `POST /v1/nodes/{name}/heartbeat` | The heartbeat marks the machine online for the fair share and returns its assigned accounts. |
| `POST /v1/runs/{run_id}/result` | A finished run frees its session on the account total and its machine. |
| `GET /v1/state` | The state shows each account once with its machines and per-machine usage. |
| `GET /v1/deploy/state` | Deploy state lists accounts by machine membership rather than a single node. |

**Console screens:** `cc`, `machines`

Accounts appear on Command Center (`/cc`) and Machines and AI (`/machines`): one account card with a breakdown per
machine, **Enable on**, **Total sessions**, **Retry sign-in**, **Remove** and **Remove from all machines**.

Changes need the `nodes.manage` permission (owner, super administrator). The shared account tables are created by
`011_shared_accounts.sql`; `015_shared_account_machine_detach.sql` records detached control-repo assignments.
