# Fleet routes

The nodes, machines and managed-account routes live in `aiflow/v4/control/features/fleet/routes.py` (request bodies in
`bodies.py`), moved out of `control/api.py` by JAIF-485 with their behaviour unchanged. Routes the docs check names for this move:

- `DELETE /v1/accounts/{account_id}`
- `DELETE /v1/nodes/{name}`
- `GET /v1/accounts/{account_id}/sign-in`
- `GET /v1/nodes/{name}/health`
- `GET /v1/nodes/{name}/operations`
- `GET /v1/nodes/{name}/operations/next`
- `PATCH /v1/accounts/{account_id}`
- `POST /v1/accounts/{account_id}/sign-in`
- `POST /v1/accounts/{account_id}/sign-in/complete`
- `POST /v1/accounts/{account_id}/sign-in/status`
- `POST /v1/nodes/join-tokens`
- `POST /v1/nodes/join-tokens/inspect`
- `POST /v1/nodes/{name}/check-probes`
- `POST /v1/nodes/{name}/join-command`
- `POST /v1/nodes/{name}/operations/{operation_id}/checkpoint`
- `POST /v1/nodes/{name}/operations/{operation_id}/result`
- `PUT /v1/nodes/{name}/work-acceptance`

The other fleet routes (`POST /v1/nodes/join`, `POST /v1/nodes/{name}/heartbeat`, `POST /v1/machines/{name}/accounts`) are described in
`docs/v4/event-architecture.md`. See that file for how `install(app, ctx, deps)` wires the routes.

Audit: the accepted join-command, work-acceptance and check-probe-token writes publish `NodeJoinCommandCreated`,
`NodeWorkAcceptanceChanged` and `CheckProbeTokenIssued`; the audit ledger writes their existing rows in the event
transaction. Refused writes still call `audit()` with `accepted=False`.

The successful node join (`node.agent_joined`), managed-account add (`ai_account.added`), management update
(`ai_account.management_changed`), sign-in restart (`ai_account.sign_in_restarted`), sign-in completion
(`ai_account.sign_in_completed`) and removal (`ai_account.removed`), node removal (`node.managed_removed`), and
node-operation queue (`node.operation_queued`) writes publish `NodeJoined`, `AiAccountAdded`,
`AiAccountManagementChanged`, `AiAccountSignInRestarted`, `AiAccountSignInCompleted`, `AiAccountRemoved`,
`NodeRemoved` and `NodeOperationQueued`. The audit ledger writes each event's existing row in the same transaction.

## Reactions

`POST /v1/nodes/join` publishes `node.agent_joined` (`NodeJoined`) and no longer runs its reactions inline. They are listeners in
`features/fleet/listeners.py`: `fleet.telemetry_node_joined` keeps the `node.joined` telemetry row in the join's transaction, and
`fleet.schedule_join_readiness` starts the machine's first readiness check from the outbox once the join has committed. The route
delivers the outbox right after the commit, so the check still starts before the node receives its answer; a listener that fails
is left for the dispatcher to retry and does not fail the join.

`POST /v1/accounts/{account_id}/sign-in/complete` publishes `ai_account.sign_in_completed`; the `account.sign_in_completed` timeline row is the in-transaction listener `fleet.telemetry_account_sign_in_completed`.
