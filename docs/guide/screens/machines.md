# Machines and AI

**Assumes:** you are signed in as an Administrator or SA (the roles that can open Machines and AI) and at least one node has sent a heartbeat.

Machines and AI (`/machines`) lists the machines that run AI work and the AI accounts on each of them.

## What you see

- **Nodes**: heartbeat health, load and how many work slots are in use.
- **AI accounts** on each node: signed in or out, quota cooldown and credential checks.
- **AI quality by role**: recent success, cost and rework for each account and role. An Owner can change the failure window and switch-off threshold; a role is paused for one account after repeated failures and returns after two successful probation tickets.

## Load-aware offers

Open a machine's details to set its CPU load per CPU, memory-use and free-disk limits. The defaults are `0.85`,
`85%` and `10%`. A shared-host CPU limit applies to every machine reporting the same hostname. Missing required
measurements or a limit breach holds new offers while existing runs continue. The screen shows the reason, value
and observation time; offers resume automatically after the recovery margin. At 70% of a machine's CPU limit,
only half of its free worker slots receive offers. The full rule and API behavior are described in
[Load-aware scheduling](../topics/load-aware-scheduling.md).

## Common tasks

- Add a machine by entering its name, role and project, then create its one-time join command. Add an AI account
  from the machine's account section by choosing a provider and creating its sign-in link and code.
- Find a node that stopped sending heartbeats: see the [node_down runbook](../runbooks/node_down.md).
- Check which accounts are signed out before work stalls: see the [node_no_ai runbook](../runbooks/node_no_ai.md).
  For capacity advice open [SA health](sa.md).
