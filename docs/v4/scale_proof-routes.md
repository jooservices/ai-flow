# Scale proof routes

The scale-proof summary and result routes. They live in `aiflow/v4/control/features/scale_proof/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-497 with their behaviour unchanged. Routes registered by `features/scale_proof/routes.py`:

- `GET /v1/scale-proof`
- `POST /v1/scale-proof/results`

Routes that stay in `control/api.py` and are named here only because their handlers call code the docs check watches; they did not move:

- `GET /v1/backups`
- `GET /v1/business/{key}/change-requests`
- `GET /v1/business/{key}/release-progress`
- `GET /v1/business/{key}/release-readiness`
- `GET /v1/deploy/ci-state`
- `GET /v1/deploy/state`
- `GET /v1/knowledge/items`
- `GET /v1/knowledge/resources`
- `GET /v1/knowledge/resources/{kind}/{resource_id}/history`
- `GET /v1/stakeholder/{key}`
- `GET /v1/state`
- `POST /v1/backups/key`
- `POST /v1/backups/run`
- `POST /v1/business/{key}/change-requests`
- `POST /v1/knowledge/items`
- `POST /v1/knowledge/items/{item_id}/{action}`
- `POST /v1/knowledge/proposals/{proposal_id}/approve`
- `POST /v1/knowledge/resources`
- `POST /v1/knowledge/resources/{kind}/{resource_id}/restore`
- `POST /v1/stakeholder/{key}/verdict`
- `PUT /v1/backups/config`

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.
