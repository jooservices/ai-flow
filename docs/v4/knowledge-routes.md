# Knowledge routes

The memory, learned-knowledge and Knowledge screen resource routes. They live in `aiflow/v4/control/features/knowledge/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-497 with their behaviour unchanged. Routes registered by `features/knowledge/routes.py`:

- `GET /v1/knowledge/items`
- `GET /v1/knowledge/resources`
- `GET /v1/knowledge/resources/{kind}/{resource_id}/history`
- `POST /v1/knowledge/items`
- `POST /v1/knowledge/items/{item_id}/{action}`
- `POST /v1/knowledge/proposals/{proposal_id}/approve`
- `POST /v1/knowledge/resources`
- `POST /v1/knowledge/resources/{kind}/{resource_id}/restore`

Audit

The item save, item moderation, policy proposal, resource commit, and policy merge writes publish audited domain events:
`knowledge.item_saved`, `knowledge.item_moderated`, `knowledge.policy_proposed`, `knowledge.resource_committed`, and
`knowledge.policy_merged`. The audit listener writes the same `write_audit` row as before.

Routes that stay in `control/api.py` and are named here only because their handlers call code the docs check watches; they did not move:

- `GET /v1/backups`
- `GET /v1/business/{key}/change-requests`
- `GET /v1/business/{key}/release-progress`
- `GET /v1/business/{key}/release-readiness`
- `GET /v1/deploy/ci-state`
- `GET /v1/deploy/state`
- `GET /v1/scale-proof`
- `GET /v1/stakeholder/{key}`
- `GET /v1/state`
- `POST /v1/backups/key`
- `POST /v1/backups/run`
- `POST /v1/business/{key}/change-requests`
- `POST /v1/scale-proof/results`
- `POST /v1/stakeholder/{key}/verdict`
- `PUT /v1/backups/config`

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.
