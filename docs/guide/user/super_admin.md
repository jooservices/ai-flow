# User guide: Super Administrator

**Assumes:** you are signed in to Command Center with the `super_admin` role, AI Flow v4 is installed and at least
one project exists or you are about to add one. You do not need a command line for any task on this page.

A Super Administrator has every permission. You do everything an [Owner](owner.md) does, and you also manage who
can sign in.

## Sign in

Open `https://<your-domain>/login`, enter your email and password and press Sign in. The first Super Administrator
is created by the installer; its password is stored once on the control machine (see [Security](../admin/security.md)).
A session lasts 12 hours.

## Daily tasks

1. Open [Command Center](../screens/cc.md) and check the incident panel. Each incident has a
   [runbook](../runbooks/index.md).
2. Open [SA health](../screens/sa.md) to see node and account health and capacity advice.
3. Open [Integrations](../screens/integrations.md) to confirm Jira and GitHub are connected.
4. Open [Projects](../screens/projects.md) to add, pause, resume or remove a project.

## Rare tasks

- Add a machine: Command Center, **Add node**, then run the one-time join command on the machine.
- Publish knowledge and approve policies: [Knowledge](../screens/knowledge.md).
- Upgrade AI Flow: [Upgrade](../admin/upgrade.md).

## Getting help

Press **Help** in the top bar: it opens the page of the screen you are on.
