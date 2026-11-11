# Jira connection route structure

`mount(app, ctx)` keeps the public installer for the four connection routes: OAuth start and callback, API-token
connection, then disconnect. `_mount_jira_oauth_routes` explicitly registers its start route before its callback
route. The Jira connection status read lives in `routes/jira_integration_status.py`; the route registry discovers it
with the other feature routers and gets its context from the request's app.

## Steps

- `_jira_auth` reads `ctx.jira_auth` and returns 503 when Jira connect is not configured.
- `_require_jira_owner` accepts the configured owner bearer token or a signed session with
  `integrations.manage`. Write actions call `_require_jira_writable`; refused write attempts keep their audit call.
- `_jira_body_of` parses request JSON by hand and accepts only objects, so model validation does not echo submitted
  secrets.
- `_jira_callback_redirect` builds the 303 return to `/integrations?jira=...` and deletes the OAuth state cookie.
- `_mount_jira_oauth_start_route` retains a saved secret when the client id is unchanged, saves changed app details,
  checks that both values exist, then stores one-use state in Redis and returns an authorize URL with a state cookie.
- `_mount_jira_oauth_callback_route` consumes pending state, checks its browser cookie, handles consent refusal, checks
  write availability, exchanges the code, then records the outcome. Success writes the audit row, emits
  `jira.connected`, closes the Jira auth incident, and redirects; a rejected exchange logs and audits before redirecting
  to the failed result.
- `_mount_jira_api_token_route` trims and validates email, token and HTTPS site before connecting. Success writes the
  audit row, emits `jira.connected`, closes the incident and returns status.
- `_mount_jira_disconnect_route` disconnects, audits the revoke result, emits `jira.disconnected`, and returns the
  updated status with the `revoked` flag.

## Routes

| Method and path | Installer | Behavior |
| --- | --- | --- |
| `GET /v1/integrations/jira` | `routes/jira_integration_status.router` | Return connection status after owner authorization. |
| `POST /v1/integrations/jira/oauth/start` | `_mount_jira_oauth_start_route` | Save or reuse OAuth app details and start a state-bound consent flow. |
| `GET /v1/integrations/jira/oauth/callback` | `_mount_jira_oauth_callback_route` | Consume state, exchange the code when allowed, and report the result by redirect. |
| `POST /v1/integrations/jira/api-token` | `_mount_jira_api_token_route` | Validate and connect the fallback email and API token. |
| `POST /v1/integrations/jira/disconnect` | `_mount_jira_disconnect_route` | Disconnect and report whether Atlassian revoked the token. |

## Adding a route

The discovered status router uses `route_deps.context(request)` for per-app context, then calls the existing owner
and auth helpers. Keep the owner check before reading auth configuration, and preserve the `checks_bearer` marker.
Put a new connection endpoint beside its route group: direct installers register with a plain call in `mount` at the
intended position; OAuth routes keep the explicit start-then-callback calls in `_mount_jira_oauth_routes`. Keep audit
writes and event publication as direct calls in endpoints so their order remains visible. Add request and side-effect
outcomes to `tests/v4/test_jira_connect_mount_parity.py` and characterize the unchanged behavior before moving a route.
