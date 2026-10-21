# Connect Jira from the UI

The owner connects AI Flow to Jira Cloud on the **Integrations** screen (`/integrations`) instead of putting
`JIRA_EMAIL` / `JIRA_API_TOKEN` in `/etc/aiflow/secrets/owner.env`. There are two ways:

- **Connect Jira** with OAuth 2.0 (3LO): the browser goes to Atlassian's consent page, Atlassian calls the control
  plane back, and the control plane stores the access token and refresh token in the secret store and refreshes
  them on its own.
- **Fallback: email and API token** for a control plane without a public callback URL.

The screen shows the connected site (name, URL, cloud id), the account, the granted scopes, when the access token
expires and a **Disconnect** button. Code: `aiflow/v4/control/jira_connect.py`; the gateway is
`aiflow/v4/control/jira.py`.

## Who may do it

Only the owner: a signed console session with role `owner`, or the owner bearer token (`AIFLOW4_OWNER_TOKEN`,
typed into the "Owner access token" field). Anyone else gets 401 (not signed in, wrong token) or 403 (another
role) and the credential stays as it was. Refused attempts are audited too.

## 1. Create the OAuth app at Atlassian (once)

1. Open <https://developer.atlassian.com/console/myapps/> and choose **Create → OAuth 2.0 integration**.
2. **Permissions → Jira API → Add**, then **Configure** and add these classic scopes only:
   - `read:jira-work`: read issues, comments, transitions and worklogs
   - `write:jira-work`: create and edit issues, comments, transitions and worklogs
   
   Do not add `manage:jira-configuration`, `manage:jira-project` or other admin scopes; AI Flow does not need
   them. `offline_access` is not listed in the console; AI Flow asks for it in the authorization URL so Atlassian
   returns a refresh token.
3. **Authorization → OAuth 2.0 (3LO) → Callback URL**:
   `<AIFLOW4_PUBLIC_URL>/v1/integrations/jira/oauth/callback`. The Integrations screen shows the exact URL.
   The owner's browser must be able to open it; it does not have to be on the internet.
4. **Settings**: copy the client id and secret.

On the Integrations screen, type the client id and client secret, then press **Connect Jira**. The secret goes to
the secret store and is never shown again; to keep the stored secret, leave the field empty. After the owner
presses **Accept** on Atlassian's page, the browser comes back to `/integrations?jira=connected`.

The authorization URL asks for `read:jira-work write:jira-work offline_access` and carries a random, single-use
`state` (kept in Redis for ten minutes and bound to the browser by an HttpOnly cookie). A callback with a missing,
wrong, expired or reused state is refused (400) and audited. If the token gives access to more than one site, the
site that matches `AIFLOW4_JIRA_SITE` is used, otherwise the first one.

The account shown is read from `/rest/api/3/myself`. If those scopes cannot read it, the screen shows the Atlassian
account id from the access token.

## 2. Fallback: email and API token

If no URL can receive the callback, create an API token at
<https://id.atlassian.com/manage-profile/security/api-tokens> for the Jira account AI Flow should act as. Then fill
in **Site** (`https://<site>.atlassian.net`), **Atlassian email** and **API token** (a password field) and press
**Save API token**. The control plane checks the pair with `/rest/api/3/myself` before it saves it. Jira then uses
basic auth against the site. The token is never sent back to the UI or the API.

## Secret store

Files under `AIFLOW4_JIRA_SECRET_DIR` (default `/etc/aiflow/secrets/jira`; directory mode 0700, files 0600,
replaced atomically):

| File | Content |
|---|---|
| `oauth-app.json` | client id and client secret of the OAuth app |
| `credential.json` | the connected credential (OAuth tokens or email + API token) and the site details shown on the screen |
| `disconnected.json` | who disconnected and when (no secret) |

control-api and control-loops both mount the directory read-write (`deploy/v4/control/compose.yaml`). Create it
once with `install -d -m 0700 /etc/aiflow/secrets/jira`. Tokens never go to the registry (DB), to events, to logs
or to API responses. Audit rows in `write_audit` (target `jira`, actions `oauth_app`, `connect`, `refresh`,
`disconnect`) carry only the kind, site, cloud id and scopes.

## Which credential is used

Every Jira call in v4 (jira_sync, the watchdog, and gateway writes such as transition, comment, fields,
description, worklog, issue creation and attachments) goes through one `Jira` gateway. Before each request the
gateway asks for the credential in this order:

1. A credential connected on the Integrations screen: OAuth bearer against
   `https://api.atlassian.com/ex/jira/<cloud id>/rest/api/3/...`, or basic auth with the fallback API token.
2. After a **Disconnect**: nothing. Jira stays disconnected until it is connected again, even when owner.env
   still holds a token.
3. Otherwise `JIRA_EMAIL` / `JIRA_API_TOKEN` from owner.env (basic auth against `AIFLOW4_JIRA_SITE`), as before.

### Moving away from owner.env

1. Leave owner.env as it is and connect Jira on the Integrations screen. The new credential takes priority right
   away and the screen shows its source.
2. Check that the Command Center still syncs tickets.
3. Remove `JIRA_EMAIL` and `JIRA_API_TOKEN` from owner.env and restart the control containers.

## Refresh

An OAuth access token is refreshed five minutes before it expires, and once more whenever Jira answers 401. The
new access token and the rotated refresh token are written back to the secret store under a file lock, so the two
control-api containers and control-loops never spend the same refresh token twice. Every refresh is audited.

When Atlassian rejects the refresh (the refresh token was revoked or expired, or the app secret changed), the
control plane:

- deletes the tokens and marks the credential `needs_reconnect` (the site details stay for the screen),
- writes an audit row (`refresh`, refused) and opens the critical incident `jira_auth` "Jira needs to be
  reconnected",
- stops jira_sync and Jira writes. The Integrations screen asks for a reconnect.

If Atlassian is only unreachable for a while, the current access token keeps being used until it expires, and the
next pass tries again. Pressing **Connect Jira** again closes the incident.

## Disconnect

**Disconnect** revokes the refresh token at Atlassian (`AIFLOW4_JIRA_OAUTH_REVOKE_URL`) when the credential is an
OAuth one and Atlassian accepts it. It then deletes `credential.json`, records `disconnected.json` and writes an
audit row. jira_sync stops: it opens the warning incident `jira_auth` "Jira is disconnected" and logs one warning,
instead of failing quietly on every pass. The OAuth app (client id and secret) stays stored, so connecting again is
one click.

## Settings

| Variable | Default |
|---|---|
| `AIFLOW4_JIRA_SECRET_DIR` | `/etc/aiflow/secrets/jira` |
| `AIFLOW4_PUBLIC_URL` | base of the callback URL |
| `AIFLOW4_JIRA_SITE` | preferred site; the owner.env fallback uses this site |
| `AIFLOW4_JIRA_OAUTH_AUTHORIZE_URL` | `https://auth.atlassian.com/authorize` |
| `AIFLOW4_JIRA_OAUTH_TOKEN_URL` | `https://auth.atlassian.com/oauth/token` |
| `AIFLOW4_JIRA_OAUTH_REVOKE_URL` | `https://auth.atlassian.com/oauth/revoke` |
| `AIFLOW4_JIRA_OAUTH_RESOURCES_URL` | `https://api.atlassian.com/oauth/token/accessible-resources` |
| `AIFLOW4_JIRA_API_BASE` | `https://api.atlassian.com/ex/jira` |

## API

| Method and path | Who | What |
|---|---|---|
| `GET /v1/integrations/jira` | owner | connection status (no secrets) |
| `POST /v1/integrations/jira/oauth/start` `{client_id, client_secret}` | owner | store the OAuth app, return `authorize_url` |
| `GET /v1/integrations/jira/oauth/callback?code&state` | Atlassian redirect | check state, exchange the code, store tokens |
| `POST /v1/integrations/jira/api-token` `{email, api_token, site}` | owner | fallback credential |
| `POST /v1/integrations/jira/disconnect` | owner | revoke and delete |

Tests: `tests/v4/test_jira_connect.py` runs against a fake Atlassian server (authorize, token, refresh, revoke,
accessible-resources and the Jira REST API).
