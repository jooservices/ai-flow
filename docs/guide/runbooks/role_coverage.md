# Runbook: role_coverage

**Assumes:** you are an Owner or Super Administrator with write access to `aiflow-control`. Raised in `control/loops.py`.

## Symptom

Incident "role ROLE has N ready accounts": warning with one ready account, critical with none. The roles checked are `master`, `intake_refiner`, `verify` and `dev`.

## Cause

Every role must have two ready accounts of different providers, so one quota or outage cannot stop the flow. Accounts are not ready when they are signed out, out of quota, or on an offline node.

## Fix

1. Read the detail: it lists the accounts of the role and which are ready.
2. Bring a missing account back: [account_quota](account_quota.md) or [node_down](node_down.md).
3. If the role has too few accounts, add one in `aiflow-control/accounts.yaml` and sign it in on a node.
4. The incident closes when two accounts are ready.
