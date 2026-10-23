# Projects

**Assumes:** you are signed in with the `projects.manage` permission (Administrator) to change
projects; other roles can read. Jira and GitHub are connected on [Integrations](integrations.md).

Projects (`/projects`) lists the Jira projects AI Flow works on.

## Common tasks

- **Add a project**: choose it from the Jira catalogue. The preflight reports what is missing before the project starts.
- **Pause** a project to stop new work; **Resume** it later.
- **Remove** a project when you no longer want AI Flow to work on it.
- Map a Jira epic to a GitHub repository.

A project whose stack has no pin in `versions.lock` gets no work: see the [config_pin runbook](../runbooks/config_pin.md).
