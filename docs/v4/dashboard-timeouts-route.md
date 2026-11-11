# Dashboard timeout route

The timeout metrics endpoint is registered by the discovered dashboard router. It keeps the existing analytics call,
default window, validation, permission check, and project scope.

## Routes

- `GET /v1/dashboard/timeouts` Returns developer-run timeout metrics for projects in the caller's Dashboard scope.

The handler uses the request's application context and the shared dashboard authentication helpers. Add dashboard read
endpoints to `control/routes/dashboard.py` and update its module route-set test when registering another route.
