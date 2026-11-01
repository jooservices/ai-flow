# Master action steps

`POST /v1/master/actions` stays registered in the runs route. Its policy, denial, escalation and action work lives in
named functions in `control/features/runs/master_actions.py`; the route calls its top-level steps directly, and their
event and audit calls stay in the plain function call graph used by the route documentation check.

The shared frozen `MasterActionState` carries the run, node, policy project, ticket and request body. `require_intent`
records the empty-intent denial. `decide` emits policy telemetry; `master_action_event`, `refuse` and
`deny_policy_decision` record action outcomes and preserve the existing HTTP errors. `escalation_options`,
`observation_key_for` and `record_escalation` prepare an escalation; `deliver_escalation` sends notices and
`publish_escalation` writes its events or audit row. The route calls `validate_ticket_action` and then the explicit
action branches `retry_or_move`, `pause_account`, `restart_run` or `open_bug`; `require_restart_target` preserves the
restart eligibility checks. The route publishes completion after a branch returns.

To add or change a step, keep its inputs explicit, preserve the order of database writes, audits, events and notices,
and call event or audit steps directly from the route's plain call graph. Add a characterization test for observable
behavior and a small unit test for each helper that can run without services.
