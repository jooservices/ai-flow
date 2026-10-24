# Machines and AI

**Assumes:** you are signed in as an Administrator or SA (the roles that can open Machines and AI) and at least one node has sent a heartbeat.

Machines and AI (`/machines`) lists the machines that run AI work and the AI accounts on each of them.

## What you see

- **Nodes**: heartbeat health, load and how many work slots are in use.
- **AI accounts** on each node: signed in or out, quota cooldown and credential checks.
- **AI quality by role**: recent success, cost and rework for each account and role. An Owner can change the failure window and switch-off threshold; a role is paused for one account after repeated failures and returns after two successful probation tickets.

## Common tasks

- Add a machine by entering its name, role and project, then create its one-time join command. Add an AI account
  from the machine's account section by choosing a provider, an optional name, sessions and jobs, and creating
  its sign-in link and code. Machines, accounts and worker counts are stored in the database and are part of every
  backup; see `docs/v4/inventory-in-database.md`.
- Find a node that stopped sending heartbeats: see the [node_down runbook](../runbooks/node_down.md).
- Check which accounts are signed out before work stalls: see the [node_no_ai runbook](../runbooks/node_no_ai.md).
  For capacity advice open [SA health](sa.md).
