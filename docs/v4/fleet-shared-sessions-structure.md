# Fleet shared accounts and sessions structure

The shared-account HTTP routes, policy and SQL now belong to `aiflow/v4/control/features/fleet/`. The legacy
`control.shared_accounts.install` entry point delegates to the feature installer to preserve its registration order.

## Steps

1. `routes_shared_sessions.py` authenticates each request, maps service refusals to the existing HTTP response, and
   publishes the existing account events.
2. `shared_sessions_service.py` validates account and sign-in rules and coordinates repository calls. It contains no
   SQL and raises `SharedAccountRefused` for expected refusals.
3. `repository.py` owns shared-account and per-machine session queries, including transaction and
   advisory-lock boundaries.
4. `bodies.py` contains the request models used by the routes.

To add a shared-account operation, add its request model to `bodies.py` when needed, put its SQL in the repository,
put validation and account rules in the service, and register a route that calls that service directly. Keep event
publication explicit in the route and preserve the route order in `shared_accounts.install`'s delegate.

## Routes

- `GET /v1/shared-accounts` lists accounts with their aggregate limits, usage, and machine state.
- `POST /v1/shared-accounts` creates a shared account and returns its current machine view.
- `PUT /v1/shared-accounts/{account_id}/session-limit` changes the total concurrent-session limit after checking active runs.
- `POST /v1/shared-accounts/{account_id}/machines` enables an account on a joined machine and starts its sign-in flow.
- `POST /v1/shared-accounts/{account_id}/machines/{node}/signin` retries sign-in on one machine with a fresh attempt.
- `DELETE /v1/shared-accounts/{account_id}/machines/{node}` removes an account from one machine, with confirmation required for its last machine.
- `DELETE /v1/shared-accounts/{account_id}` removes a shared account from all machines after confirmation.
- `POST /v1/accounts/{account_id}/signin-prompt` stores a pending machine's provider sign-in link and device code.
- `POST /v1/accounts/{account_id}/signin-complete` completes the current machine sign-in attempt and updates the account probe state.
