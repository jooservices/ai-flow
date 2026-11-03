# Shared account route installation structure (JAIF-546)

`install` in `control/shared_accounts.py` keeps its public signature and registers six named route groups in order:
account listing and creation, session limit, machine enable and sign-in retry, machine removal, account removal, then
node sign-in prompt and completion. `shared_accounts_routes.py` holds the DB work for those handlers. The route
handlers still call `publish_for` and `ctx.events.emit` directly, in their original positions.

`sync_managed_accounts` runs the existing account sync and creates missing signed-out account rows for managed shared
accounts. `account_or_404` loads an account from the repository or returns the existing 404. `account_view` builds the
account summary, including active runs and machine details. Route handlers perform console authorization directly.
`create_account_record` validates the id and provider, rejects duplicates, inserts the shared account, and syncs it.
`set_session_limit_record` takes the account
session lock, refuses limits below active sessions, and persists the new limit. `enable_machine_record` checks the
account and joined machine, clears any exclusion, adds the assignment, and syncs. `retry_signin_record` increments a
pending sign-in attempt and clears its old prompt.

`detach_machine` removes an assignment and records the exclusion under the account lock. `delete_account` removes the
shared account and related rows only when the shared record is managed. `remove_machine_record` checks the machine
account screen and last-machine confirmation rules, then detaches and conditionally deletes. `remove_account_record`
requires confirmation, then deletes a managed record or detaches every machine while retaining the configured source.
`signin_prompt_record` checks the machine assignment, HTTPS link and device code before storing the prompt and expiry.
`begin_signin_complete` verifies the current attempt, marks a pending assignment signed in, and accepts a race only
when it is already signed in. `finish_signin_complete` updates the account's probe status after successful sign-in.

To add a route, put its registration in the owning group and its DB decisions in a module-level helper with explicit
inputs. Preserve group order and keep event publication directly in the route handler. Extend the parity table and
test new helpers with the existing DB fakes.

## Routes

The split preserves `GET /v1/shared-accounts`, `POST /v1/shared-accounts`,
`PUT /v1/shared-accounts/{account_id}/session-limit`,
`POST /v1/shared-accounts/{account_id}/machines`,
`POST /v1/shared-accounts/{account_id}/machines/{node}/signin`,
`DELETE /v1/shared-accounts/{account_id}/machines/{node}`, `DELETE /v1/shared-accounts/{account_id}`,
`POST /v1/accounts/{account_id}/signin-prompt` and
`POST /v1/accounts/{account_id}/signin-complete`. The shared account helpers are also reached by
`POST /v1/accounts/{account_id}/probe`, `GET /v1/deploy/state`, `GET /v1/state`,
`POST /v1/version-releases/claim` and `POST /v1/work/claim`.
