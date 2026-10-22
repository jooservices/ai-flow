# Runbook: release_verification

**Assumes:** you are an Owner or Super Administrator and can reach the node. Raised in `control/api.py`.

## Symptom

Critical incident "node NAME rejected release vX.Y.Z". A rollout may halt ([fleet_rollout](fleet_rollout.md)).

## Cause

The node could not verify the release: the checksum or the cosign signature did not match, or the installer failed. A damaged download is the usual reason; a changed release asset is the serious one.

## Fix

1. Read the detail the node sent.
2. Retry once. If it fails again, compare the release's `SHA256SUMS` with the file on GitHub and do not install the file.
3. If the release was changed after it was published, treat it as a security incident ([security](../admin/security.md)).
