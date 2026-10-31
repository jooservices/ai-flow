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
