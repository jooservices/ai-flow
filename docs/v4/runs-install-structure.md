# Runs routes structure (JAIF-548)

`install(app, ctx, deps)` in `aiflow/v4/control/features/runs/routes.py` calls two route-group installers in the original order: `_install_run_routes` (the run lifecycle) and `_install_master_routes` (the master AI). The long bodies moved out of the handlers:

| Part | Module | Owns |
|---|---|---|
| Autonomy report | `autonomy.py` | the weekly report: tickets finished without a person, escalations that needed one, repeated failures, quota awareness; `tests/v4/test_runs_autonomy_parity.py` pins 18 recorded cases (queries, parameters, result) |
| Plan outcome | `plan_outcome.py` | whether the master's plan was valid, the invalid-plan counts and the incident for repeated invalid plans |
| Master actions | `master_actions.py` | the steps of a master action; `perform_action` runs the allowed action |

The registrations of all route installs are pinned by `tests/v4/test_route_install_registrations.py`.

## Routes

Run lifecycle: `POST /v1/runs/{run_id}/heartbeat`, `POST /v1/runs/{run_id}/result`, `POST /v1/runs/{run_id}/release`.
Master: `GET /v1/master/context`, `POST /v1/master/flow-bugs`, `POST /v1/master/plan-outcome`, `GET /v1/master/autonomy`, `PUT /v1/master/autonomy/budget`, `GET /v1/master/report`, `POST /v1/master/actions`.
