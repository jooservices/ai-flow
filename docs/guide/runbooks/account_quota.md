# Runbook: account_quota

**Assumes:** you are an Owner or Super Administrator and can sign in to the AI provider. Raised in `control/loops.py`.

## Symptom

Warning incident "account ID is out of quota", "is signed out" or "fails its check". Its tickets go to the other accounts.

## Cause

- out of quota: the provider's limit was reached; the detail shows when it resets;
- signed out: the sign-in on the node expired or was revoked;
- fails its check: the worker's probe of the account fails (see the last error in the detail).

## Fix

1. Out of quota: wait for the reset time, or use another account of the same role. The account returns by itself ("cooldown ended").
2. Signed out: sign the account in again on its node, as the `aiflow-worker` user with the account's own home.
3. Failing check: read the error; fix the CLI or the network on the node.
4. Check [role_coverage](role_coverage.md) so the flow keeps two ready accounts per role.
