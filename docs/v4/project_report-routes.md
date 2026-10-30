# Project report routes

The project versions, progress report, close and study-case routes. They live in `aiflow/v4/control/features/project_report/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-495 with their behaviour unchanged. Routes registered by `features/project_report/routes.py`:

- `GET /v1/projects/{key}/report`
- `GET /v1/projects/{key}/study-cases`
- `GET /v1/projects/{key}/versions`
- `POST /v1/projects/{key}/close`

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.
