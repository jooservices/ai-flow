# Control API router registry

New control API features can live in `aiflow/v4/control/routes/<feature>.py`. Each feature module exposes a FastAPI
`APIRouter` as `router` and may set an integer `priority`; modules without one use priority `100`. The control app
discovers modules in this package when it is created and includes routers in ascending priority order, using the
module name to break ties.

This registry is the route registration point for new feature routers. Adding a feature module requires no central
list update. Existing inline API routes and explicit `install` or `mount` route modules remain in place while the
control API is extracted incrementally. This internal foundation adds no console screen or API route by itself.
