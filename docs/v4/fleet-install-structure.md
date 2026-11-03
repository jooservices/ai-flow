# Fleet route installation structure

Fleet routes are registered by small installers in their original order. Each `_register_*` function keeps a route's
decorator and handler together. `install` in `features/fleet/routes.py` calls the route groups and returns the same
helper names for existing callers. `FleetRouteHelpers` carries helpers shared by account, operation and probe routes.

## Route installers

### `install_node_lifecycle_routes`

- POST `/v1/nodes/join-tokens`
- POST `/v1/nodes/join-tokens/inspect`
- POST `/v1/nodes/join`
- POST `/v1/nodes/{name}/heartbeat`
- GET `/v1/nodes/{name}/health`

### `install_join_command_routes`

- POST `/v1/nodes/{name}/join-command`

### `install_managed_account_routes`

- POST `/v1/machines/{name}/accounts`
- PATCH `/v1/accounts/{account_id}`
- POST `/v1/accounts/{account_id}/sign-in`
- GET `/v1/accounts/{account_id}/spec`
- GET `/v1/accounts/{account_id}/sign-in`
- POST `/v1/accounts/{account_id}/sign-in/status`
- POST `/v1/accounts/{account_id}/sign-in/complete`
- DELETE `/v1/accounts/{account_id}`
- PUT `/v1/machines/{name}/load-limits`

### `install_node_admin_routes`

- PUT `/v1/nodes/{name}/work-acceptance`
- DELETE `/v1/nodes/{name}`
- POST `/v1/nodes/{name}/operator-interventions`

### `install_node_operation_admin_routes`

- POST `/v1/nodes/{name}/operations`
- GET `/v1/nodes/{name}/operations`

### `install_check_probe_routes`

- POST `/v1/nodes/{name}/check-probes/token`
- POST `/v1/nodes/{name}/check-probes`

### `install_node_operation_agent_routes`

- GET `/v1/nodes/{name}/operations/next`
- POST `/v1/nodes/{name}/operations/{operation_id}/checkpoint`
- POST `/v1/nodes/{name}/operations/{operation_id}/result`

## Adding a route

Put the handler and its decorator in a named `_register_*` function in the module for its route group. Call that
function from the group's `install_*_routes` function at the position matching the API's registration order. Pass
shared values through the group's parameters or `FleetRouteHelpers`; keep event publication and audit calls visible
from the handler. Update the registration parity fixture and this route list when the route contract changes.
