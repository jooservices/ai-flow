# Integrations

**Assumes:** you are signed in with the `integrations.manage` permission (Owner or Super Administrator), you are an
administrator of your Jira site and of your GitHub organization.

Integrations (`/integrations`) connects AI Flow to Jira and GitHub. Secrets are stored on the control machine and
are never shown.

## Common tasks

- **Connect Jira**: use OAuth 2.0 (recommended) or an API token. The page shows the connection state; AI Flow refreshes the token by itself.
- **Disconnect** Jira: stops the Jira sync and all writes to Jira until you connect again.
- **Connect GitHub**: create or import a GitHub App, choose its installation and refresh the accessible repositories.

## If Jira says "needs to be reconnected"

Press **Connect Jira** again. See the [runbook](../runbooks/jira_auth.md).
