# Stakeholder

**Assumes:** you are signed in as an Administrator, Product owner or Stakeholder, and the project lists its environment links in its configuration.

Stakeholder (`/stakeholder`) opens the configured environments of a project and records whether you accept the result.

## Common tasks

1. Open the environment link and try the change.
2. Record acceptance or your feedback. AI Flow writes it to the Jira ticket.

The screen sends the decision through `POST /v1/stakeholder/{key}/verdict`.
