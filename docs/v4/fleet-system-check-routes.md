# Fleet System check routes

**Console screens:** System check (`system-check`)

The System check report, its manual run and the support bundle are registered by the fleet feature
(`features/fleet/routes_system_check.py`, installed last by `install` in `features/fleet/routes.py`) instead of by
`control/system_check_api.py`. Behaviour is unchanged; the readiness logic stays in `control/system_check.py`.

## Routes

- `GET /v1/system-check` returns readiness for nodes serving the caller's visible projects; with console login off, a valid optional session still scopes the report and an anonymous caller sees all nodes.
- `POST /v1/system-check/run` runs the readiness check on every node now and returns the new report; it needs the `nodes.manage` permission.
- `GET /v1/support-bundle` downloads an uncached JSON attachment for nodes serving the caller's visible projects, with the same optional-session scope when console login is off.

## Adding a route

Add a `_register_*` function with the decorator and handler together, call it from `install_system_check_routes`, and
update `tests/v4/fixtures/route-install-registrations.jsonl` and this list.
