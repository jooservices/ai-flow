# Runbook: verify_no_verdict

**Assumes:** you are an Owner or Super Administrator and can edit the ticket in Jira. Raised in `control/verify_no_verdict.py`.

## Symptom

Warning incident "KEY cannot be verified: every eligible account ended without a verdict". The ticket sits in In Verify and no verification account takes it.

## Cause

Every account that could verify the ticket has ended verification of it twice in a row without a VERDICT line, so none is offered the ticket again. Typical causes are a verification prompt the model cannot follow, a missing tool on the verifying machine, or a quota or provider problem that cuts runs short. The detail names the ticket.

## Fix

1. Open the ticket on [Delivery](../screens/delivery.md) and read the last verify run summaries.
2. If a tool or service is missing on the verifying machine, fix it with [node_tools](node_tools.md) or [node_services](node_services.md); if quota is the cause see [account_quota](account_quota.md).
3. Send the ticket back to development (rework) or verify it by hand and merge. A new development submission starts a new round and lets the accounts try again.
4. The incident closes when the ticket leaves verification or an account that is not barred can receive it.
