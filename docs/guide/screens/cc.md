# Command Center

**Assumes:** you are signed in with any role; the Dashboard shows only the projects you belong to. Adding nodes needs the `nodes.manage` permission (Administrator
or SA).

Command Center (`/cc`) is the first screen. It shows the state of the whole flow.

## What you see

- Summary tiles and alerts at the top.
- **Nodes**: each machine and the accounts working on it. Each account is one worker taking ready tickets. Open a node for its details and operation history.
- **Live events** from the registry.
- **AI accounts**: quota, state and the filter for accounts that need attention. Quota belongs to an account.
- **Capacity plan**: seven-day quota capacity, the limiting AI, recent assignment explanations and estimate errors. Open any ticket to see its saved estimate and assignment reason in the ticket drawer, including assignments outside the short Dashboard list.
- **Jira and GitHub disagree**: tickets whose Jira status and pull request have differed for more than five minutes, with the status the flow will set and why it is not fixed yet. The flow fixes them itself; see `docs/v4/merged-means-done.md`. An approved ticket waiting to be merged (**Ready to merge**) counts as in flight.
- **Runs**: running now and the last 30 finished.
- **Incidents**: opened and closed by the watchdog. Critical ones are emailed. Each kind has a [runbook](../runbooks/index.md).
- **Versions**: the tool versions pinned in `aiflow-control/versions.lock` and what each node reports.

## Common tasks

- **Add node** (Owner and Super Administrator): choose the name, role and project, then show the one-time command (valid for five minutes) to run on the new machine. No provisioning key is typed; your signed-in session authorizes it.
- **Finish setting up AI Flow**: after the first Administrator exists, a panel at the top of the Work tab lists the remaining first-run steps (Jira, GitHub, machine, AI accounts, project, sample ticket). Each step says in plain words what is missing, links to Integrations, Machines and AI, or Projects, and can be skipped and resumed. The panel disappears once every step is ready and stays hidden afterwards. See `docs/v4/first-run-setup.md`.
- Open an incident title to read its detail, then follow its runbook.

## If it looks wrong

If the page says it cannot load, check [the control plane](../runbooks/flow_idle.md) health and your session (sign in again).
