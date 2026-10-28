# Business

**Assumes:** you are signed in as an Administrator, Product owner, BA or Business, a project is selected and Jira is connected.

Business (`/business`) is for people who decide, not for people who build.

## What you see

- **Epics** with their progress.
- **Needs your answer**: tickets in Needs owner. Answer in Jira, not here.
- **Change requests**: stories with the label `change-request`, with the AI impact assessment. The owner decides whether to move one on from Backlog.
- **Release readiness and progress** of each Jira version.

An unreleased version without an attached passing scale proof shows a blocking reason here. See [Scale proof](../topics/scale-proof.md) for the measured limits and what the run simulates.

The version progress panel reads `GET /v1/business/{key}/release-progress`. It is read-only; release actions
run through the release workflow.

## Common tasks

- Send a change request: fill the form; AI Flow creates a Story in the project with the label `change-request`.
