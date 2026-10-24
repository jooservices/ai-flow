# System readiness check

**Console screens:** `system-check`, `projects`

The Console has a **System check** screen at `/system-check`. It shows machine-wide conditions and each configured
project's conditions on every machine assigned to that project. The same readiness snapshot is available from
`GET /v1/system-check`; `GET /v1/support-bundle` downloads a safe JSON support bundle that includes it. The Support
screen also creates a scanned archive with `POST /v1/support/bundles` and downloads it through
`GET /v1/support/bundles/{bundle_id}/download`.

Each row has one result: **ready**, **fixed automatically**, **needs an action**, or **not possible on this machine**.
Rows that need a person include one direct action. Credential checks expose only whether a credential is present
and valid; they never include credential values or provider output. Machine results are rebuilt from known
condition IDs and result states, so node supplied names, details, actions, and extra fields are not stored in the
readiness snapshot or returned in reports and support bundles.

The node agent checks available disk, writable work and worker-state directories, control API connectivity, GitHub
HTTPS with certificate validation through configured proxy settings, tool versions, service health, and sanitized
credential probe state. The control plane combines these with project stack pins, required services, repository and
base-branch settings, and the AI accounts assigned to the machine.
Known stack tools and declared test services use the existing node preparation routines: pinned tools are installed
automatically, missing/stopped test services are started or recreated, and stale worktrees are removed only when
the control plane confirms they belong to completed or spent work. When that cleanup restores the 5 GiB minimum,
the disk row reports **fixed automatically**. Space below the minimum that remains after cleanup gives the exact
free-space action.

The control plane checks after a node joins, after a project is added, when a node reports a new release, and once
per hour. Those scheduled checks revalidate the GitHub App installation, access to every configured repository,
each repository's base branch, and the Jira credential with read-only integration calls. Their sanitized results are
reused between heartbeats. An owner or Super Administrator can also use **Run now**. A newly failing condition
alerts only when it blocks work; a healthy transition or a change marked as not affecting work does not send an
alert.

Before offering work, the control plane filters out any project with a failed machine or project condition. A
credential failure, including an unconfirmed credential status, blocks only work requested by that account, so a
different healthy account on the same node can still serve the project. A machine or project without its first
readiness snapshot is held until the check completes. The Projects screen names the machine and failed condition
that prevents work.

The support bundle includes the sanitized readiness results and omits raw node heartbeat tool and metric maps. The
clean-install acceptance test waits for each newly installed node's machine and project result, then confirms that
the support bundle contains that result.

## API contracts

The readiness gate and trigger points touch these control API routes:

- `GET /v1/system-check` returns the snapshot; `POST /v1/system-check/run` starts a manual check.
- `POST /v1/support/bundles` builds and scans an archive containing the sanitized readiness snapshot;
  `GET /v1/support/bundles/{bundle_id}/download` returns the archive after review.
- `POST /v1/runs/{run_id}/result` stores any sanitized worker support log for ticket-specific bundles.
- `POST /v1/nodes/join` returns the node token promptly and schedules the first check; `POST /v1/nodes/{name}/heartbeat`
  stores the node's reported conditions, schedules provider probes in the background and never waits for them.
- `POST /v1/projects` schedules a check for the new project; `GET /v1/projects` adds the per-machine readiness
  and the failed condition that blocks work to each project. `DELETE /v1/projects/{key}`,
  `GET /v1/projects/{key}/repositories` and `PUT /v1/projects/{key}/repositories` keep their behaviour; repository
  results feed the repository conditions.
- `GET /v1/work/candidates` and `POST /v1/work/claim` omit and refuse a project whose machine or account fails a
  condition it needs. `GET /v1/version-releases/candidates`, `POST /v1/version-releases/claim` and
  `GET /v1/projects/{key}/versions` apply the same gate to version-release runs.
- `GET /v1/master/candidate`, `GET /v1/nodes/{name}/operations`, `POST /v1/accounts/{account_id}/probe`,
  `POST /v1/nodes/{name}/check-probes/token`, `POST /v1/events`, `POST /v1/integrations/github/imports` and
  `GET /v1/integrations/github/imports/{pending_id}/installations` keep their contracts; they only use the
  shared readiness helpers.
