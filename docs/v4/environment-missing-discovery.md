# Missing environment route discovery

The projects feature owns the missing-environment report handler and its `APIRouter` in
`aiflow/v4/control/features/projects/discovered_environment_missing.py`. The registry reaches it through
`aiflow/v4/control/routes/environment_missing.py`, which exposes the feature router as `router`.

To add a related discovered endpoint, place its handler in the owning feature router, read the application context
from the request through `route_deps.context`, and expose that router from a module under `control/routes/`.
Keep authentication and existing response behavior at the route edge; project reports are loaded once before the
session's project scope is applied.

## Routes

- `GET /v1/environment/missing` returns the missing environment reports allowed by the current console session.
