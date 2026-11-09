# Broad exception handlers

The broad-except guard (`python3 -m aiflow.v4.core.broad_except`) accepts a broad `except Exception` only with
a reason in the form `# noqa: BLE001 - <reason>`. The baseline in docs/v4/broad-except-baseline.json may only
shrink. This change explains nine handlers and pins their behaviour with tests without changing behaviour.

## Explained handlers

The nine explained handlers are in these modules (all under `aiflow/v4/control`): `memory.py`, `routes/support.py`,
`features/console_login/service.py`, `flow_doctor.py`, `features/runs/master_actions.py`,
`features/version_release/claim_steps.py`, `features/projects/routes_project_listing.py`,
`features/projects/routes_project_environment.py` and `features/console/routes_console_user_create.py`. Each keeps its
behaviour; only the reason of the broad catch is written down.

## Routes

The documentation check requires a changed documentation page to name every route of each module that changed. The
list below is that coverage: a route is listed because its module changed, not because its own handler has a broad
catch (for example `GET /v1/knowledge/items` has none).

- `GET /v1/ba-chat/candidate`
- `GET /v1/integrations/jira/oauth/callback`
- `GET /v1/knowledge/items`
- `GET /v1/master/candidate`
- `GET /v1/memory/search`
- `GET /v1/projects/catalog`
- `GET /v1/support/bundles/{bundle_id}/download`
- `GET /v1/version-releases/candidates`
- `GET /v1/work/candidates`
- `POST /v1/console/users`
- `POST /v1/integrations/jira/api-token`
- `POST /v1/knowledge/items`
- `POST /v1/knowledge/items/{item_id}/{action}`
- `POST /v1/master/actions`
- `POST /v1/master/plan-outcome`
- `POST /v1/nodes/{name}/heartbeat`
- `POST /v1/nodes/{name}/release-failures`
- `POST /v1/runs/{run_id}/result`
- `POST /v1/version-releases/claim`
- `POST /v1/work/claim`
- `PUT /v1/projects/{key}/environment`
- `PUT /v1/role-health/settings`
- `POST /v1/incidents/{incident_id}/resolve-and-requeue` Records the coordinator's resolution of a ticket hand-off and requeues the ticket (JAIF-354).
