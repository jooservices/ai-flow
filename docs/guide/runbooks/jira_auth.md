# Runbook: jira_auth

**Assumes:** you are an Owner or Super Administrator and an administrator of your Jira site. Raised in `control/jira_connect.py`.

## Symptom

Incident "Jira needs to be reconnected" (critical), "Jira is disconnected" or "Jira is not connected" (warning). The Jira sync and all writes to Jira are stopped, so tickets do not move.

## Cause

- The Jira refresh token was rejected (revoked or expired): reconnect needed.
- An owner pressed Disconnect.
- No credential was ever set.

## Fix

1. Open [Integrations](../screens/integrations.md).
2. Press **Connect Jira** and approve access on the Atlassian page.
3. The incident closes ("Jira credential works again") and the sync restarts.
