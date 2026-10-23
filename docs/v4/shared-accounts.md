# Shared AI accounts

An AI account exists apart from machines. The owner enables it on several machines in Command Center; the flow counts
its sessions and quota once, across all of them, and never runs more sessions than the account's total limit.

## Use it (Command Center)

1. **Accounts → New shared account**: give an id (`claude-02`), the provider and the total number of concurrent
   sessions. The account now exists without any machine.
2. Open the account and choose **Enable on** a machine. The machine shows *Sign-in needed*.
3. The node agent starts the provider's login command in that machine's private account home and reports the
   provider's sign-in **link and code** to Command Center. Open the link and enter the code. The provider CLI stores
   the credential on that machine. Each machine signs in once. The first passing account check from the machine marks
   it signed in and removes the link and code.
4. **Total sessions** changes the limit at any time. Accounts defined in `aiflow-control/accounts.yaml` can be shared
   the same way; their first machine stays defined in the repo and keeps its previous capacity as the limit until you
   change it.
5. **Remove** takes the account off one machine only; other machines keep using it. Removing the last machine, or
   **Remove from all machines**, asks for an on-screen confirmation and deletes the account.

The account list shows the account once (quota, status, prediction, `used/total sessions`). The account panel shows a
breakdown per machine below: online or offline, sessions now, sessions in the last 24 hours, sign-in state.

## How the limit and the spread work

- Sessions are the running runs of the account id, whichever machine runs them. Every claim, including a version
  release, inserts the run under a per-account database lock and checks both the total limit and the machine's fair
  share, so concurrent workers cannot take another online machine's capacity (`409`, ticket lease released).
- A machine takes at most its fair share, `ceil(limit / machines online and signed in)`, while another such machine
  is online. A machine that is offline (no heartbeat for 75 s) drops out, so the others' share grows and the work
  moves to them. The limit still holds.
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

Changes need the `nodes.manage` permission (owner, super administrator). The migration is `011_shared_accounts.sql`.
