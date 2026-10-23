# Runbook: version_skew

**Assumes:** you are an Owner or Super Administrator and a rollout may be needed. Raised in `control/rollout.py`.

## Symptom

Warning incident "node NAME is incompatible with the control plane". The node gets no new work.

## Cause

The node runs a release that the control/node version policy does not allow with the current control release (see `docs/v4/fleet-upgrade.md`).

## Fix

1. Read the reason in the detail.
2. Let the fleet rollout give the node the current release, or install the release on the node.
3. The incident closes when the node runs a compatible release.
