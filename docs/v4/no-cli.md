# No CLI for end users

The owner principle is that end users do not use CLI. New end-user features must have a UI path; CLI remains
for developers and CI. This inventory is part of TESTFLOW-77 and records the current v4 path, including cases
where the UI still asks an owner to copy and run a command. Ticket references are kept when known; no Jira keys
are guessed.

| Operation | How it works today | UI or replacement |
| --- | --- | --- |
| Install the control plane and prepare a host | The owner runs one command on a new Ubuntu 22.04/24.04 host: `aiflow4 install control --version vX.Y.Z --domain D --admin-email you@example.com` (through `aiflow4-control-install` the first time), and enters tokens with `aiflow4 secret set NAME` (hidden input); see [Install the control plane](control-install.md). | TESTFLOW-70. There is no console yet when the first install runs, so this one stays a command; a UI for upgrades and secrets is not built yet. |
| Provision a VM and enroll a node | The owner creates/prepares the VM with the provider tooling or the fleet Ansible playbook. Command Center issues a one-use `curl ... \| sudo sh` join command, which the owner must run on the host. | TESTFLOW-55 tracks VM creation; TESTFLOW-71 tracks the node onboarding path. The current join command is shown in Command Center but still requires shell use. |
| Install or repair node tools and services | An authenticated owner queues **Install** or **Repair** in the Command Center node drawer; the node agent applies the desired state and the UI shows the operation result. | UI available (TESTFLOW-54). |
| Assign an AI account to a node | The owner uses **Machines and AI** to choose the provider, sessions, and jobs. The assigned node starts the provider's sign-in flow in that account's private home. | UI available; see [Machines and AI management](machines-and-ai.md); TESTFLOW-76. |
| Sign an AI account in | The owner opens the provider authorization link and enters its provider-issued code in **Machines and AI**. The node stores credentials locally and reports when its account check succeeds. | UI available; see [Machines and AI management](machines-and-ai.md). |
| Connect or disconnect Jira | The owner uses **Integrations** (`/integrations`) for OAuth or the API-token fallback, then can disconnect there. Initial OAuth-app registration is done in Atlassian's UI. | UI available; see [Connect Jira](jira-connect.md). |
| Connect GitHub or manage its repositories | The owner uses **Integrations** to create/import a GitHub App, select an installation, and refresh accessible repositories. | UI available; see [Connect GitHub](github-connect.md). |
| Add, pause, resume, or remove a project | The owner uses **Projects** (`/projects`); changes are committed to `aiflow-control` by the GitHub App. Project pause/resume is already a UI action. | UI available; see [Project management](projects.md). |
| Change an existing project's settings or node assignment | Fields not covered by the current project form are changed by editing `projects/<KEY>.yaml` in `aiflow-control` and committing it. | Proposed – ticket to be created by the lead after merge. |
| Manage runtime secrets and environment settings | The owner creates or edits files under `/etc/aiflow/secrets/` and `/etc/aiflow/projects/`, sets values in the control environment, and restarts affected services. Jira credentials and the GitHub App have dedicated UI flows, but general runtime secrets do not. | Proposed – ticket to be created by the lead after merge. |
| Upgrade or recover the control plane and fleet | The owner starts a control release with `aiflow4 install control --version vX.Y.Z` (or the auto-deployer does); a failed health check rolls back by itself, and `aiflow4 install control --rollback` returns to the previous release. Nodes then receive the release through the canary and rolling waves. | TESTFLOW-70 and TESTFLOW-71 track the deployment and node release workflows; the normal node rollout is automated after control deployment. |
| Pause or drain a node | There is no node pause/drain action in the console. An operator can stop the node agent with `systemctl`, which takes it offline rather than safely draining active work. | Proposed – ticket to be created by the lead after merge. |
| View operation and system logs | The Command Center shows install/repair operation results, `/sa` shows health and links to OpenObserve (`/o2`) for flow events. Detailed host or service logs still require `journalctl` or `docker compose logs`. | UI available for operation results, health, and flow events. A console viewer for host/service logs is Proposed – ticket to be created by the lead after merge. |
| Run developer and CI workflows | Developers and CI use repository commands, release scripts, and test commands. | CLI is appropriate for developers and CI under the owner principle. |

The proposed rows have no replacement UI or ticket yet. Under the lead's AC2 decision, the lead will create their
Backlog tickets after this change merges; these proposals intentionally have no invented TESTFLOW keys. Existing
ticket rows and operations that already have a UI remain identified above. TESTFLOW-81 records this inventory.
