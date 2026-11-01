# Projects routes

The project, environment and GitHub/Jira integration routes. They live in `aiflow/v4/control/features/projects/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-490 with their behaviour unchanged. Routes registered by `features/projects/routes.py`:

- `DELETE /v1/integrations/github/connections/{connection_id}`
- `DELETE /v1/projects/{key}`
- `GET /v1/environment/missing`
- `GET /v1/environment/scan`
- `GET /v1/integrations/github`
- `GET /v1/integrations/github/connections`
- `GET /v1/integrations/github/imports/{pending_id}/install`
- `GET /v1/integrations/github/imports/{pending_id}/installations`
- `GET /v1/integrations/github/installation-settings`
- `GET /v1/integrations/github/manifest/callback`
- `GET /v1/integrations/github/manifest/start`
- `GET /v1/integrations/github/setup`
- `GET /v1/projects`
- `GET /v1/projects/catalog`
- `GET /v1/projects/{key}/environment`
- `GET /v1/projects/{key}/repositories`
- `POST /v1/environment/removals`
- `POST /v1/environment/scan`
- `POST /v1/integrations/github/connections/token`
- `POST /v1/integrations/github/connections/{connection_id}/test`
- `POST /v1/integrations/github/imports`
- `POST /v1/integrations/github/imports/{pending_id}/connect`
- `POST /v1/projects`
- `POST /v1/projects/preflight`
- `POST /v1/projects/{key}/pause`
- `POST /v1/projects/{key}/services`
- `PUT /v1/integrations/github/connections/{connection_id}/token`
- `PUT /v1/projects/{key}/environment`
- `PUT /v1/projects/{key}/repositories`

Routes that stay in `control/api.py` and are named here only because their handlers call code the docs check watches (the shared
schemas and helpers); they did not move:

- `GET /v1/github/checks`
- `GET /v1/github/ci-log`
- `GET /v1/github/git/{git_path:path}`
- `GET /v1/github/pr`
- `POST /v1/github/comment`
- `POST /v1/github/delete-branch`
- `POST /v1/github/git/{git_path:path}`
- `POST /v1/github/merge`
- `POST /v1/github/pr`
- `POST /v1/github/token`
- `POST /v1/github/update-branch`
- `POST /v1/reviews/{key}/answer`
- `POST /v1/reviews/{key}/approve`
- `POST /v1/reviews/{key}/ask-ai`
- `POST /v1/reviews/{key}/reject`
- `POST /v1/reviews/{key}/rework`
- `POST /v1/reviews/{key}/skip-gate`

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.

## Audit

Successful GitHub connection and repository writes publish `github.repository_snapshot_recorded`,
`github.repository_access_changed`, `github.app_connected`, `github.token_connection_added`,
`github.token_connection_replaced`, `github.connection_removed`, `github.manifest_started`,
`github.manifest_created`, `github.app_import_verified`, `github.app_install_opened`,
`github.installation_settings_opened` and `github.connection_assigned`. Environment removals publish
`environment.removals_recorded`. Successful project configuration writes publish `project.repositories_saved`,
`project.environment_saved`, `project.environment_scanned`, `project.added`, `project.services_changed`,
`project.pause_changed` and `project.removed`. The audit ledger listener writes the same `write_audit` rows as before.
Refused project writes still call `audit()` directly.

The progress report helper in `control/report.py` was split (its requirement-coverage section became `_coverage_markdown`) with no change to
the routes that render it: `GET /v1/projects/{key}/report` and `POST /v1/projects/{key}/close`.
