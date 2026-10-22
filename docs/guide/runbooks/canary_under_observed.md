# Runbook: canary_under_observed

**Assumes:** you are an Owner or Super Administrator and the canary auto-deploy is on. Raised in `control/canary.py`.

## Symptom

Info incident "Canary vX.Y.Z promoted after the maximum observation time with too few runs". It is a note; it is not emailed.

## Cause

The canary node completed fewer runs than required during the maximum observation time, so the release was promoted with little evidence.

## Fix

1. No urgent action. Watch the fleet after the promotion for [fleet_rollout](fleet_rollout.md) and failed runs.
2. If this repeats, lower the required run count or give the canary node more work (`docs/v4/canary-autodeploy.md`).
