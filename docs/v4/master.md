# Master autonomy

The master account runs one fenced control-wide review cycle at a time. When `accounts.yaml` has no explicit master
role, the first configured `senior` account receives `master` by default. Explicit role assignments remain
authoritative. The owner can view and change the master's daily model-time budget in the Command Center at `/cc`.
The default budget is 60 minutes per UTC day; setting it to zero disables master cycles. The control plane refuses
new cycles after the budget is used. A cycle with nothing to review does not make a model call or consume the
budget.

## What the master reviews

The fenced `GET /v1/master/context` snapshot contains:

- Every open incident, including offline nodes, quota and account health, role coverage, stuck tickets and run clocks.
- Tickets in `Needs owner`, including whether they were already escalated.
- Running ticket runs with elapsed time and the project's `work_minutes` target, warn and hard limits.
- Offline nodes and accounts out of quota, failing their check, or temporarily paused by the master.
- Repeated TESTFLOW failure reports from the fenced scan described in [Repeated flow failures](flow-bugs.md).

The worker runs `prompts/v4/master.md`, returns a JSON action plan, and submits each action to
`POST /v1/master/actions`. The control API checks the active run and fence, applies the owner escalation policy,
records accepted and refused requests in the audit log, and performs only the safe actions listed below. Tool calls
are denied for master runs, so all effects go through the control API. The master reviews the snapshot for tickets
beyond the applicable project hard limit. Quota, authentication, offline-node and missing-tool symptoms stay
incidents; they are not converted into TESTFLOW Bugs.

## Safe actions, escalation and audit

Allowed actions are:

- `retry` a ticket after a failed, blocked, quota or lost run, by returning it to To Do.
- `move_to_todo` when a ticket has no active run and is not Done.
- `pause_account` with a temporary pause of up to 24 hours for an account with a quota or health issue.
- `restart_run` after its project hard limit; the control plane releases the target run's lease, marks it lost and
  returns its ticket to To Do.
- `open_bug` only for an eligible repeated TESTFLOW failure. The control API applies the threshold and tracker
  deduplication from [Repeated flow failures](flow-bugs.md).

Every accepted or refused request is recorded in `write_audit` and emitted as a `master.action` event with the
reason. Lease fencing and the control-repo policy hook remain in force. No other Jira writes or infrastructure
changes are available through the master action API.

When the master needs a person to choose, it sends one alert through the configured owner channels. The alert says
why a person is needed and lists the available choices. An observation key suppresses repeat messages after
successful delivery; failed channels remain pending and are retried on a later master cycle without resending to
channels that already succeeded. Protected infrastructure, secret and production-release requests continue to be
escalated by the policy hook.

## Daily budget and malformed plans

The master settings table stores the daily time budget and the UTC-day usage. Only time spent in the model counts;
an idle review has a recorded `model_time_s` of zero. The model gets one retry with a strict JSON-format reminder
after returning malformed output. A valid plan resets the invalid-plan streak. The control plane opens the
`master_invalid_plan` incident after three invalid cycles in a row or five invalid cycles within one hour. A later
valid plan closes that incident.

## Weekly autonomy report

The Command Center autonomy panel shows AI-finished tickets, owner decisions with their reasons and options,
repeated flow Bugs, and weekly counts over the previous eight weeks. The report is available to signed-in console
readers through `GET /v1/master/autonomy` and `GET /v1/master/report`; only owners and super administrators can
change the budget.

## Lease and provider handoff

The global `master:global` Redis lease allows one cycle at a time and uses the normal run heartbeat and fencing
checks. Any ready account with the master role may take the next cycle. A cooldown, signed-out or error account,
offline node, temporary master pause, exhausted daily budget or expired lease excludes that account. After a quota
or lost-lease failure, a ready account from another provider is preferred for one cycle; when none is available,
any other ready master account may continue. Successful cycles remain at least one minute apart, while other
failures use the usual run backoff.
