# Runs routes

The worker run heartbeat, result and release routes and the master AI routes. They live in `aiflow/v4/control/features/runs/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-479 with their behaviour unchanged. Routes registered by `features/runs/routes.py`:

- `GET /v1/master/autonomy`
- `GET /v1/master/context`
- `GET /v1/master/report`
- `POST /v1/master/actions`
- `POST /v1/master/flow-bugs`
- `POST /v1/master/plan-outcome`
- `POST /v1/runs/{run_id}/heartbeat`
- `POST /v1/runs/{run_id}/release`
- `POST /v1/runs/{run_id}/result`
- `PUT /v1/master/autonomy/budget`

Routes that stay in `control/api.py` and are named here only because their handlers call code the docs check watches; they did not move:

- `GET /v1/github/checks`
- `GET /v1/github/ci-log`
- `GET /v1/github/git/{git_path:path}`
- `GET /v1/github/pr`
- `GET /v1/reviews/export.csv`
- `POST /v1/github/comment`
- `POST /v1/github/delete-branch`
- `POST /v1/github/git/{git_path:path}`
- `POST /v1/github/merge`
- `POST /v1/github/pr`
- `POST /v1/github/token`
- `POST /v1/github/update-branch`
- `POST /v1/github/webhook`
- `POST /v1/reviews/{key}/answer`
- `POST /v1/reviews/{key}/approve`
- `POST /v1/reviews/{key}/ask-ai`
- `POST /v1/reviews/{key}/reject`
- `POST /v1/reviews/{key}/rework`
- `POST /v1/reviews/{key}/skip-gate`
- `POST /v1/work/claim`

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.

Quality offsets in the same change, behaviour unchanged: `_jira_auth_blocked` (System check), `_update_json_manifest` and `_open_blocker`
(version release), `_restart_worker_operation`, `_decrypt_rollback_snapshot`, `_runtime_setting_target` and `_failed_rollback_services`
(node operations) and `_run_check` (version release worker) were split out of larger functions.

Routes that call the functions split out above and are named for the docs check:

- `POST /v1/nodes/{name}/heartbeat`
- `POST /v1/projects`
- `POST /v1/system-check/run`
- `POST /v1/version-releases/{version_id}/complete`

## Audit

Successful `PUT /v1/master/autonomy/budget` and `POST /v1/master/actions` requests publish `master.daily_budget_updated`
and `master.action_completed`; the audit ledger listener writes the same accepted `write_audit` rows as before. The
budget update and its event share a transaction. An accepted `escalate_to_owner` action publishes
`master.escalation_requested` and `master.escalation_notified`; they commit with the update of the escalation's delivered
channels. A refused action, an escalation action other than `escalate_to_owner` and the partial-result audit that is
followed by an error remain direct `audit()` calls. Successful flow-bug writes publish `flow_bug.issue_created`,
`flow_bug.repeat_commented` and `flow_bug.intake_moved`; refused or failed Jira writes keep their direct refused audit rows.
