# Integrations

**Assumes:** you are signed in as an Administrator or SA (the roles that can open Integrations), you are an
administrator of your Jira site and of your GitHub organization.

Integrations (`/integrations`) connects AI Flow to Jira and GitHub. Secrets are stored on the control machine and
are never shown.

## Common tasks

- **Connect Jira**: use OAuth 2.0 (recommended) or an API token. The page shows the connection state; AI Flow refreshes the token by itself.
- **Disconnect** Jira: stops the Jira sync and all writes to Jira until you connect again.
- **Connect GitHub**: create or import a GitHub App, choose its installation and refresh the accessible repositories. GitHub Apps are recommended.
- **Add an access token**: paste a personal or fine-grained token. AI Flow verifies it with GitHub, stores it and never shows it again. A token has broader reach and no bot identity, so the page warns when you use one.
- **Test**, **Replace** (a new App or token) or **Remove** each GitHub connection. A connection that repositories use cannot be removed until they are reassigned.

## If Jira says "needs to be reconnected"

Press **Connect Jira** again. See the [runbook](../runbooks/jira_auth.md).
