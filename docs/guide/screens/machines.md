# Machines and AI

**Assumes:** you are signed in with the `node.operations.read` permission and at least one node has sent a heartbeat.

Machines and AI (`/machines`) lists the machines that run AI work and the AI accounts on each of them.

## What you see

- **Nodes**: heartbeat health, load and how many work slots are in use.
- **AI accounts** on each node: signed in or out, quota cooldown and credential checks.

## Common tasks

- Find a node that stopped sending heartbeats: see the [node_down runbook](../runbooks/node_down.md).
- Check which accounts are signed out before work stalls: see the [node_no_ai runbook](../runbooks/node_no_ai.md).
  For capacity advice open [SA health](sa.md).
