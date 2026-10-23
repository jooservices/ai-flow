# Administrator guide: upgrade

**Assumes:** you are a Super Administrator with sudo on the control machine, AI Flow v4 is installed, you have a
newer published release `vX.Y.Z`, and you have a recent [backup](backup.md).

## Upgrade the control plane

Run the install command again with the newer version:

```sh
sudo aiflow4 install control --version vX.Y.Z --domain aiflow.example.com
```

The installer first runs the pre-upgrade check. If it blocks (exit code `9`), nothing changed and the control
plane keeps running the current release; a critical `upgrade_blocked` incident names the reasons, see its
[runbook](../runbooks/upgrade_blocked.md). After the migrations it deploys the new release one API replica at a
time and checks health. If the check fails the previous release is restored (exit code `5`).

Database migrations only move forward. A rollback restores the previous release, not the previous database
structure, which is why you back up first.

## Upgrade the nodes

Nodes upgrade by themselves in a rolling rollout: one canary node first, then waves, with a health check after
each. If a node fails, the rollout halts and the nodes that changed return to their previous release. You see
`fleet_rollout` and `fleet_rollback` incidents: [rollout halted](../runbooks/fleet_rollout.md),
[rollback](../runbooks/fleet_rollback.md).

A node more than the allowed distance behind the control plane gets no new work until it is upgraded
([version_skew](../runbooks/version_skew.md)).

## After the upgrade

Open Command Center and check the footer shows the new version and no critical incident is open.
