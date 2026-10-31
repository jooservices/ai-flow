# Composition root

`aiflow/v4/control/api.py` only assembles the control app (JAIF-503): it builds the context, the lifespan and the middleware, then calls the
feature installers. The helpers every route group shares live in `control/app_helpers.py`, and the join-token, GitHub gateway and git proxy
helpers in `control/gateway_helpers.py` (moved without changes). Routes that call the moved gateway helpers and are named for the docs check:

- `POST /v1/nodes/{name}/heartbeat`
- `POST /v1/projects`
- `POST /v1/system-check/run`

See `docs/v4/event-architecture.md` for the feature layout.
