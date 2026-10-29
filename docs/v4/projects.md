# Manage projects in the console

Open **Projects** in the AI Flow console to see configured project keys, repositories, active or paused state,
priority and mirrored ticket count. An authenticated owner can add, pause, resume or remove a project from this
screen. Project files that could not be loaded appear in an alert with the filename and reason; correct the named
file in aiflow-control so the project can be assigned to machines again.

## Add a project

Choose the connected Jira site, then select a Jira project from the site's project list or enter its project key
or a URL on that site. Choose a repository from the repositories visible to the GitHub App, its base branch
(default `develop`), stack, checks, priority, parallel limit and merge method. Run preflight before saving.

Preflight reads Jira workflow statuses and GitHub repository branches. It lists any missing required Jira
statuses (`Backlog`, `Intake`, `To Do`, `In Progress`, `In Verify`, `Needs owner`, `Done`) and refuses to save
when one is missing. It also refuses a base branch that does not exist. These checks do not write to Jira or
GitHub project repositories. The toolchain result shows each node's reported stack tools and which tools are
missing; an unready node receives the project requirements at its next heartbeat and cannot claim work until
its toolchain passes the normal gate.

Saving writes the versioned project policy in `projects/<KEY>.yaml` to the `aiflow-control` default branch through
the GitHub App, producing a bot commit. The file contains no machine list: eligible worker machine assignments are
stored in the control database and are returned to each node at its next heartbeat. Creating a project makes it
available to current worker machines; enrolling a machine through **Machines and AI** records its selected project.
Jira sync includes configured active projects automatically.

The generated YAML has this shape (with the selected checks, stack, priority, limit and merge method):

```yaml
key: NEWAPP
repo: jooservices/new-app
base: develop
stack: python-3.12
checks:
  - uv run --frozen pytest
merge:
  method: squash
limits:
  parallel: 2
  work_minutes:
    target: 30
    warn: 45
    hard: 60
priority: 5
paused: false
```

Older project files may have a `nodes` list. The one-time inventory import copies those assignments to the control
database and removes the list from the control repository. Do not add or edit `nodes` in project YAML; see
[Database-backed inventory](inventory-in-database.md).

## Pause and remove

Pause commits `paused: true` to the project's YAML. The project stops syncing in `jira_sync` and its tickets stop
appearing in candidates for new claims. Runs already in progress remain leased and can finish normally. Resume
commits `paused: false`.

Remove deletes only `projects/<KEY>.yaml` from `aiflow-control` in a bot commit. The project stops syncing and
receiving candidates after the control repo refresh. Jira settings, Jira tickets and the GitHub source
repository are left alone.

Each add, pause/resume and remove action is recorded in `write_audit` with the console owner, action, project,
result and bot commit SHA when available.
