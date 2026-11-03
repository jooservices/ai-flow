# Run result structure

## Routes

`POST /v1/runs/{run_id}/result` is registered by `aiflow/v4/control/features/runs/routes.py`. It checks node and fence ownership, then calls the named steps below in order. `run_finished` remains an explicit call in the route so the completion event stays visible to the documentation check.

## Steps

- `_prepare_run_result` removes worker supplied provenance and quota reservation fields, extracts the raw support log, records verify criteria when applicable, and applies the load failure policy for the current owner.
- `_finish_run_result` finishes the inbox result and determines whether account data and account status may be updated.
- `_update_run_result_account` stores the diagnostic log, refreshes quota data, records authentication failures, and updates account cooldown or idle status in the existing order.
- `_record_run_result_missing` records missing tools or environment items for developer runs and changes an unaccepted item to a failed outcome.
- `_capture_run_result_followups` captures run knowledge, calculates duration, and records developer time on Jira when eligible.
- `_evaluate_run_result_health` evaluates the completing account and, for a verify run, the different developer account it judged.

## Adding a step

Add a module level helper with explicit inputs and one responsibility. Call it from `run_result` at the phase it belongs to, preserving the existing order and keeping event publication visible in the route. Extend `tests/v4/test_run_result_parity.py` with a fake collaborator trace before changing the handler.
