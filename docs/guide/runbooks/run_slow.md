# Runbook: run_slow

**Assumes:** you are an Owner or Super Administrator. Raised in `control/loops.py`.

## Symptom

Warning incident "KEY passed N minutes": a run is still working after the warning time of its project (default 45 minutes).

## Cause

A large ticket, a slow test suite, or a worker waiting on a tool or service. Nothing is wrong yet; the hard limit is next (default 60 minutes).

## Fix

1. Open the ticket on [Delivery](../screens/delivery.md) and read the live timeline.
2. If it is making progress, wait. If it is stuck, look at the node ([node_services](node_services.md), [node_tools](node_tools.md)).
3. Consider splitting the ticket in Jira.
4. The incident closes when the run finishes. See also [run_hard_limit](run_hard_limit.md).
