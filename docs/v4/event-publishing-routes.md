# Routes that publish domain events

Every route below publishes at least one domain event (`publish_for`, or a call that reaches it). The audit ledger listener writes the
route's `write_audit` row from the event, and some routes have further reactions (listeners). The event types are listed in
[events.md](events.md); the rows each route writes are described in the `Audit` and `Reactions` sections of its route page
(for example [fleet-routes.md](fleet-routes.md), [projects-routes.md](projects-routes.md), [runs-routes.md](runs-routes.md)).

This page exists because the documentation check treats a change to `platform/events/wiring.py` as a change to every route that
publishes an event: such a change must update a page that names them, and this is that page.

## business

- `POST /v1/business/{key}/change-requests`
- `POST /v1/stakeholder/{key}/verdict`

## console

- `DELETE /v1/notifications/channels/{channel_id}`
- `PATCH /v1/console/users/{email}/disabled`
- `POST /v1/console/users`
- `POST /v1/licensing/activate`
- `POST /v1/licensing/offline`
- `PUT /v1/console/users/{email}`
- `PUT /v1/licensing/limits`
- `PUT /v1/notifications/channels`
- `PUT /v1/notifications/preferences`

## github_ci_log

- `GET /v1/github/ci-log`

## github_fenced

- `GET /v1/github/pr`
- `POST /v1/github/comment`
- `POST /v1/github/delete-branch`
- `POST /v1/github/pr`
- `POST /v1/github/update-branch`

## knowledge

- `POST /v1/knowledge/items`
- `POST /v1/knowledge/items/{item_id}/{action}`
- `POST /v1/knowledge/proposals/{proposal_id}/approve`
- `POST /v1/knowledge/resources`
- `POST /v1/knowledge/resources/{kind}/{resource_id}/restore`

## load_policy_api

- `PUT /v1/machines/{name}/load-limits`

## projects

- `DELETE /v1/integrations/github/connections/{connection_id}`
- `DELETE /v1/projects/{key}`
- `GET /v1/integrations/github`
- `GET /v1/integrations/github/imports/{pending_id}/install`
- `GET /v1/integrations/github/installation-settings`
- `GET /v1/integrations/github/manifest/callback`
- `GET /v1/integrations/github/manifest/start`
- `GET /v1/integrations/github/setup`
- `GET /v1/projects/catalog`
- `GET /v1/projects/{key}/repositories`
- `POST /v1/environment/removals`
- `POST /v1/environment/scan`
- `POST /v1/integrations/github/connections/token`
- `POST /v1/integrations/github/imports`
- `POST /v1/integrations/github/imports/{pending_id}/connect`
- `POST /v1/projects`
- `POST /v1/projects/preflight`
- `POST /v1/projects/{key}/pause`
- `POST /v1/projects/{key}/services`
- `PUT /v1/integrations/github/connections/{connection_id}/token`
- `PUT /v1/projects/{key}/environment`
- `PUT /v1/projects/{key}/repositories`

## scale_proof

- `POST /v1/scale-proof/results`

## shared_accounts

- `DELETE /v1/shared-accounts/{account_id}`
- `DELETE /v1/shared-accounts/{account_id}/machines/{node}`
- `POST /v1/shared-accounts`
- `POST /v1/shared-accounts/{account_id}/machines`
- `POST /v1/shared-accounts/{account_id}/machines/{node}/signin`
- `PUT /v1/shared-accounts/{account_id}/session-limit`
