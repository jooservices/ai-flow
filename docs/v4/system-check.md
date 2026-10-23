# System readiness check

The Console has a **System check** screen at `/system-check`. It shows machine-wide conditions and each configured
project's conditions on every machine assigned to that project. The same readiness snapshot is available from
`GET /v1/system-check`; `GET /v1/support-bundle` downloads a safe JSON support bundle that includes it.

Each row has one result: **ready**, **fixed automatically**, **needs an action**, or **not possible on this machine**.
Rows that need a person include one direct action. Credential checks expose only whether a credential is present
and valid; they never include credential values or provider output. Machine results are rebuilt from known
condition IDs and result states, so node supplied names, details, actions, and extra fields are not stored in the
readiness snapshot or returned in reports and support bundles.

The node agent checks available disk, writable work and worker-state directories, control API connectivity, tool
versions, service health, and sanitized credential probe state. The control plane combines these with project
stack pins, required services, repository and base-branch settings, and the AI accounts assigned to the machine.
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
