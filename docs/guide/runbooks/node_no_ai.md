# Runbook: node_no_ai

**Assumes:** you are an Owner or Super Administrator with write access to `aiflow-control`. Raised in `control/loops.py`.

## Symptom

Warning incident "node NAME has no AI account". The node is online but takes no ticket.

## Cause

The node joined but no AI account is assigned to it in `aiflow-control/accounts.yaml` (the detail adds when the node is not described in `aiflow-control/nodes/`).

## Fix

1. Assign at least one account to the node in `aiflow-control/accounts.yaml`.
2. Sign the account in on the node as the `aiflow-worker` user, with the account's own home directory (see `docs/v4/node-join.md`).
3. The incident closes ("accounts assigned") on the next watchdog pass.
