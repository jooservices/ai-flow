# Knowledge routes

The memory, learned-knowledge and Knowledge screen resource routes. Item handlers live in
`aiflow/v4/control/features/knowledge/routes_items.py` and are exposed to route discovery by
`control/routes/knowledge_items.py`. Resource and proposal handlers remain installed by
`features/knowledge/routes.py`.

## Routes

- `GET /v1/knowledge/items` returns the authorized knowledge dashboard and item search for a project and query.
- `GET /v1/knowledge/resources` lists managed knowledge resources and pending proposals.
- `GET /v1/knowledge/resources/{kind}/{resource_id}/history` returns a resource's saved version history.
- `POST /v1/knowledge/items` saves a learned-knowledge item for the authenticated console user.
- `POST /v1/knowledge/items/{item_id}/{action}` applies a moderation action to a learned-knowledge item.
- `POST /v1/knowledge/proposals/{proposal_id}/approve` approves a pending policy proposal.
- `POST /v1/knowledge/resources` saves a versioned knowledge resource or proposes a policy change.
- `POST /v1/knowledge/resources/{kind}/{resource_id}/restore` restores a prior resource version.

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
