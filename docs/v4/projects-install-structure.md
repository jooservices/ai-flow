# Projects route install structure

`aiflow/v4/control/features/projects/routes.py:install` builds the frozen route runtime and calls the route groups in
registration order. Each group in `routes_*.py` names its route installers explicitly; each route installer keeps
the FastAPI decorator beside its handler. Shared behavior lives in module-level helpers in `routes.py` with the
runtime passed explicitly. The managed project listing is discovered from
`control/routes/project_listing.py`; this installer registers the catalog route only. To add an installer route,
place its decorated handler in the matching group, call its installer in
the group’s existing order, and add the method and path below.

## Routes

### `install_integrations_routes`

- GET `/v1/integrations/github`
- GET `/v1/integrations/github/connections`
- POST `/v1/integrations/github/connections/token`
- POST `/v1/integrations/github/connections/{connection_id}/test`
- PUT `/v1/integrations/github/connections/{connection_id}/token`
- DELETE `/v1/integrations/github/connections/{connection_id}`
- GET `/v1/integrations/github/manifest/start`
- GET `/v1/integrations/github/manifest/callback`
- GET `/v1/integrations/github/setup`
- POST `/v1/integrations/github/imports`
- GET `/v1/integrations/github/imports/{pending_id}/installations`
- GET `/v1/integrations/github/imports/{pending_id}/install`
- POST `/v1/integrations/github/imports/{pending_id}/connect`
- GET `/v1/integrations/github/installation-settings`

### `install_project_listing_routes`

- GET `/v1/projects/catalog`

### `install_project_repository_routes`

- GET `/v1/projects/{key}/repositories`
- PUT `/v1/projects/{key}/repositories`

### `install_project_environment_routes`

- GET `/v1/projects/{key}/environment`
- PUT `/v1/projects/{key}/environment`

### `install_environment_routes`

- GET `/v1/environment/missing`
- GET `/v1/environment/scan`
- POST `/v1/environment/scan`
- POST `/v1/environment/removals`

### `install_project_lifecycle_routes`

- POST `/v1/projects/preflight`
- POST `/v1/projects`
- POST `/v1/projects/{key}/services`
- POST `/v1/projects/{key}/pause`
- DELETE `/v1/projects/{key}`
