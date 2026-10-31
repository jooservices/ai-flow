# Backup routes

The encrypted-backup configuration, status, key download and run-now routes. They live in `aiflow/v4/control/features/backups/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-497 with their behaviour unchanged. Routes registered by `features/backups/routes.py`:

- `GET /v1/backups`
- `POST /v1/backups/key`
- `POST /v1/backups/run`
- `PUT /v1/backups/config`

## Audit

These writes are audited through `backup.configured`, `backup.key_downloaded` and `backup.run_requested` by the audit ledger listener.
The audit row is field-for-field identical to the row written before.
Configure and run audit rows now commit with their changes.

Routes that stay in `control/api.py` and are named here only because their handlers call code the docs check watches; they did not move:

- `GET /v1/business/{key}/change-requests`
- `GET /v1/business/{key}/release-progress`
- `GET /v1/business/{key}/release-readiness`
- `GET /v1/deploy/ci-state`
- `GET /v1/deploy/state`
- `GET /v1/knowledge/items`
- `GET /v1/knowledge/resources`
- `GET /v1/knowledge/resources/{kind}/{resource_id}/history`
- `GET /v1/scale-proof`
- `GET /v1/stakeholder/{key}`
- `GET /v1/state`
- `POST /v1/business/{key}/change-requests`
- `POST /v1/knowledge/items`
- `POST /v1/knowledge/items/{item_id}/{action}`
- `POST /v1/knowledge/proposals/{proposal_id}/approve`
- `POST /v1/knowledge/resources`
- `POST /v1/knowledge/resources/{kind}/{resource_id}/restore`
- `POST /v1/scale-proof/results`
- `POST /v1/stakeholder/{key}/verdict`

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.

Size offsets made in the same change, with no change to behaviour: `jira_sync` (`_mirror_versions`, `_mirror_ticket`) and the
System check (`_clean_machine_rows`, `_rebuilt_credentials`, `_keep_provider_baseline`, `_alert_blocking_changes`) were split into smaller
functions. Routes that call this code and are named for the docs check:

- `GET /v1/projects`
- `GET /v1/support-bundle`
- `GET /v1/system-check`
- `GET /v1/version-releases/candidates`
- `POST /v1/nodes/{name}/heartbeat`
- `POST /v1/projects`
- `POST /v1/support/bundles`
- `POST /v1/system-check/run`
- `POST /v1/version-releases/claim`

Further size offsets, behaviour unchanged: `_keep_unrecovered_holds` (load policy hysteresis), `_member_problem` (rollout) and
`_secret_targets` (runtime settings). Routes that call this code and are named for the docs check:

- `GET /v1/master/candidate`
- `GET /v1/work/candidates`
- `POST /v1/runtime-settings`
- `POST /v1/work/claim`
- `PUT /v1/machines/{name}/load-limits`
