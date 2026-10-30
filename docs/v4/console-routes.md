# Console routes

The console session, user, view-as, layout, preference, notification and licensing routes. They live in `aiflow/v4/control/features/console/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-495 with their behaviour unchanged. Routes registered by `features/console/routes.py`:

- `DELETE /v1/console/layouts/{page}`
- `DELETE /v1/notifications/channels/{channel_id}`
- `GET /v1/console/layouts`
- `GET /v1/console/preferences`
- `GET /v1/console/session`
- `GET /v1/console/users`
- `GET /v1/licensing`
- `GET /v1/licensing/status`
- `GET /v1/licensing/usage`
- `GET /v1/licensing/usage.csv`
- `GET /v1/notifications`
- `PATCH /v1/console/users/{email}/disabled`
- `POST /v1/console/users`
- `POST /v1/console/view-as`
- `POST /v1/console/view-as/stop`
- `POST /v1/licensing/activate`
- `POST /v1/licensing/offline`
- `POST /v1/notifications/channels/{channel_id}/test`
- `PUT /v1/console/layouts/{page}`
- `PUT /v1/console/preferences`
- `PUT /v1/console/users/{email}`
- `PUT /v1/licensing/limits`
- `PUT /v1/notifications/channels`
- `PUT /v1/notifications/preferences`

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.

The System check readiness export of `POST /v1/support/bundles` was moved into `_readiness_export` in `control/support_bundle.py`
(a size offset for the new `install` function); the route and its output are unchanged.
