# Runbook: ticket_stuck

**Assumes:** you are an Owner or Super Administrator and can edit the ticket in Jira. Raised in `control/loops.py`.

## Symptom

Warning incident "KEY failed N runs in a row" (N is 3 or more). The ticket keeps going back to To Do and failing. Retries wait up to 30 minutes.

## Cause

The same ticket fails again and again: an unclear ticket, a broken test, a repository problem, or an infrastructure failure. The detail shows the last error and the account.

## Fix

1. Open the ticket on [Delivery](../screens/delivery.md) and read the timeline and last run summary.
2. If the ticket is unclear, answer or correct it in Jira.
3. If the cause is the environment (a tool, a service, quota), fix it with [node_tools](node_tools.md), [node_services](node_services.md) or [account_quota](account_quota.md).
4. If you need time, put the label `aiflow-hold` on the ticket.
5. The incident closes when a run produces work again. In the test project, repeated causes are turned into flow-bug tickets (`docs/v4/flow-bugs.md`).
