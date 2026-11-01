# Master run structure

`run_master` in `aiflow/v4/worker/master.py` runs one fenced master cycle for the account that already holds the claim. The steps below are module-level functions called in this order. Lease loss, a refused fencing token, and an unexpected worker failure stay in `run_master`.

1. `prepare_master_workspace` creates `work/.master` and `logs/runs`.
2. `run_master` builds the heartbeat and starts it, then `run_master_cycle` reviews the snapshot.
3. `load_master_snapshot` calls `GET /v1/master/context` and `POST /v1/master/flow-bugs`, and stores `flow_bug_reports` on the snapshot.
4. `master_review_progress` sets the heartbeat progress. When the snapshot needs no decision, `finish_idle_master` records the idle result.
5. `master_model_timeout` turns the autonomy budget into the model timeout. `build_master_run_spec` builds the read-only run.
6. `bind_master_argv` (through `prefixed_master_argv`) prefixes the adapter command. `record_master_ai_progress` updates the heartbeat from each model event.
7. `master_quota_fields` reads the newest reported quota. `finish_master_model_error` records a provider failure on the first response.
8. `retry_invalid_master_plan` asks once more while time remains. `finish_master_retry_error` records a provider failure on that retry.
9. `record_master_usage` stores token totals. `apply_master_plan` calls `POST /v1/master/plan-outcome`.
10. `master_action_payload` keeps the allowed action fields. `perform_master_actions` submits each action with `POST /v1/master/actions`.

While the cycle runs, the heartbeat renews the lease with `POST /v1/runs/{run_id}/heartbeat`.

A new step is a named function in `aiflow/v4/worker/master.py` with a docstring. Call it explicitly from `run_master` or from the step that owns that phase. Add a focused test in `tests/v4/test_master_run_parity.py`.
