# Runbook: upgrade_blocked

**Assumes:** you are a Super Administrator with sudo on the control machine. Raised in `control/rollout.py`.

## Symptom

Critical incident "control upgrade to vX.Y.Z blocked". The installer exited with code 9; nothing changed.

## Cause

The pre-upgrade check found a reason not to upgrade (for example a migration problem or a node that would fall outside the version policy). The detail lists the reasons.

## Fix

1. Fix each listed reason.
2. Run the upgrade again ([upgrade](../admin/upgrade.md)). The incident closes when the check passes.
3. The control plane keeps running its current release meanwhile.
