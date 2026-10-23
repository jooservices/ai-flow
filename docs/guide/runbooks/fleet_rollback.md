# Runbook: fleet_rollback

**Assumes:** you are an Owner or Super Administrator and a rollout was halted ([fleet_rollout](fleet_rollout.md)). Raised in `control/rollout.py`.

## Symptom

Incident "rollback of vX.Y.Z succeeded" (warning) or "FAILED" (critical).

## Cause

After a halted rollout the nodes that already changed are returned to their previous release. FAILED means some did not come back.

## Fix

1. Succeeded: no action; investigate the cause of the halt.
2. FAILED: the detail lists the nodes still on the target release. Install the previous release on each (see `docs/v4/fleet-upgrade.md`).
3. If such a node later reports its previous release, the rollback is marked succeeded and the incident closes.
