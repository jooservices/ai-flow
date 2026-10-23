# Command Center

**Assumes:** you are signed in with any role; the Dashboard shows only the projects you belong to. Adding nodes needs the `nodes.manage` permission (Administrator
or SA).

Command Center (`/cc`) is the first screen. It shows the state of the whole flow.

## What you see

- Summary tiles and alerts at the top.
- **Nodes**: each machine and the accounts working on it. Each account is one worker taking ready tickets. Open a node for its details and operation history.
- **Live events** from the registry.
- **AI accounts**: quota, state and the filter for accounts that need attention. Quota belongs to an account.
- **Runs**: running now and the last 30 finished.
- **Incidents**: opened and closed by the watchdog. Critical ones are emailed. Each kind has a [runbook](../runbooks/index.md).
- **Versions**: the tool versions pinned in `aiflow-control/versions.lock` and what each node reports.

## Common tasks

- **Add node** (Owner and Super Administrator): creates a one-use token valid for five minutes with the command to run on the new machine.
- Open an incident title to read its detail, then follow its runbook.

## If it looks wrong

If the page says it cannot load, check [the control plane](../runbooks/flow_idle.md) health and your session (sign in again).
