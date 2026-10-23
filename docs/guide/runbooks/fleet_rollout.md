# Runbook: fleet_rollout

**Assumes:** you are an Owner or Super Administrator and a rolling node upgrade is under way. Raised in `control/rollout.py`.

## Symptom

Critical incident "rollout of vX.Y.Z halted: node NAME failed its health check". No node receives the release any more.

## Cause

A node in the canary or in a wave did not report the new release as healthy in time, or refused it.

## Fix

1. Read the detail: release, previous release, wave, failed node, reason, and the rollback plan.
2. The rollback starts by itself. Watch its result in [fleet_rollback](fleet_rollback.md).
3. Find why the node failed (its log, [release_verification](release_verification.md)) and fix it before you start the release again.
