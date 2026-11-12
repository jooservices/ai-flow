# Watchdog loop structure

`run` in `aiflow/v4/control/watchdog.py` runs every watchdog check. A check that raises is logged and the later checks still run. The checks stay in the order below.

## Machines, tools and orphaned tickets

`run_machine_and_orphan_checks` calls these checks, in order:

1. `node_heartbeats` marks an online node offline when its heartbeat has stopped, and closes that incident when the heartbeat returns.
2. `version_skew` asks the fleet rollout to watch nodes that are outside the release policy.
3. `node_tools` opens a warning when an online node is missing a tool one of its projects needs, or an AI tool for its accounts.
4. `node_services` opens a warning when an online node's declared test service is unhealthy.
5. `tool_and_environment_waits` runs the tool-wait passes, then the environment-wait passes and the daily scan.
6. `nodes_without_accounts` opens a warning for an online node that has no AI account, and closes it when an account is assigned or the node leaves.
7. `orphaned_tickets` sends an In Progress ticket with no run back to To Do after ten quiet minutes.

## Stuck work, leases and quota

`run_flow_and_quota_checks` then calls these checks, in order:

1. `stuck_tickets` opens a warning after three dev or verify failures in a row, and leaves tool-wait warnings to the tool-wait pass.
2. `verify_no_verdict` opens one warning when every eligible account has at least one current-round verification without a verdict; the two-result offer bar remains separate.
3. `retry_and_deadline_passes` applies retry holds, answers overdue owner questions, then checks stack pins and an idle flow.
4. `lost_leases_and_run_clocks` records a run whose lease is gone and opens the warn and hard-limit incidents for a run that is still owned.
5. `account_cooldowns` returns an account to idle when its cooldown has ended, and keeps a warning open while an account is cooling down, signed out or failing its check.
6. `role_coverage` warns when a flow role has ready accounts from fewer than two providers.
7. `quota_windows` opens and closes the warning for a provider quota window.
8. `quota_forecasts` stores each account's quota forecast and opens a warning when the forecast runs out before reset.

Each check lives in `aiflow/v4/control/watchdog_checks/<name>.py`. The SQL those checks run lives in `aiflow/v4/control/repositories/watchdog.py`.

## Adding a check

Add a module `aiflow/v4/control/watchdog_checks/<name>.py` with one function `<name>(ctx)`. Put a new SQL statement in `aiflow/v4/control/repositories/watchdog.py` and call it from the check. Import the function in `aiflow/v4/control/watchdog.py` and insert it into `CHECKS` where it should run. `run_machine_and_orphan_checks` runs the first seven entries. `run_flow_and_quota_checks` runs the rest. Move `MACHINE_AND_ORPHAN_COUNT` when the new check belongs with the first group.

## Routes

No route calls the watchdog. The control loop is what runs it.
