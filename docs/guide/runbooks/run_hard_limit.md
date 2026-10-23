# Runbook: run_hard_limit

**Assumes:** you are an Owner or Super Administrator. Raised in `control/loops.py`.

## Symptom

Critical incident "KEY passed N minutes": a run exceeded the hard limit of its project (default 60 minutes).

## Cause

The run is stuck or the ticket is too big for one run. A worker that stopped sending heartbeats loses its lease and the ticket returns to To Do by itself.

## Fix

1. Open the run on [Delivery](../screens/delivery.md); check the node and the account in [Command Center](../screens/cc.md).
2. If the node is unhealthy follow [node_down](node_down.md).
3. Split the ticket in Jira if it keeps hitting the limit, or put it on hold (`aiflow-hold`).
4. The incident closes when the run ends.
