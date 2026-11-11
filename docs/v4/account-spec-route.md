# Account specification route

The node-facing account specification is exposed by `control/routes/account_spec.py` and mounted when the control
API discovers routers in `control/routes/`. The handler reads the application context from the request, authenticates
through the shared node-token dependency, and checks whether the account runs on that node before returning its
provider, tier, model and effort. New routes in this package expose a module-level `router` for discovery.

## Routes

- `GET /v1/accounts/{account_id}/spec` returns the model specification for an account assigned to the authenticated node.
