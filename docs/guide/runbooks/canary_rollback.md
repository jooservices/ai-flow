# Runbook: canary_rollback

**Assumes:** you are an Owner or Super Administrator and the canary auto-deploy is on. Raised in `control/canary.py`.

## Symptom

Critical incident "Canary rollback: vX.Y.Z on NODE ..." and a `[flow-bug]` Jira ticket for the suspected commit.

## Cause

The canary node had a higher infrastructure failure rate than the previous release, or it never installed the candidate. The release was rolled back automatically.

## Fix

1. Read the detail: canary and baseline run counts and failure rates, and the suspected commit.
2. Fix the cause through the flow-bug ticket. The release is not promoted to the fleet.
3. See `docs/v4/canary-autodeploy.md` for the thresholds.
