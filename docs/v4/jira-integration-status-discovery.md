# Jira integration status route

The owner-protected Jira status read is a discovered feature router. It keeps the existing API and gets its
`ApplicationContext` from each request's app, so separate app instances do not share context.

## Steps

- `jira_status` obtains context with `route_deps.context(request)`, checks the owner bearer token or
  `integrations.manage` session, then returns `_jira_auth(ctx).status()`.
- `checks_bearer` marks the handler so the console sign-in gate lets bearer requests reach the route's own token
  check.
- The route registry discovers `jira_integration_status.router` under `control/routes/`; no installer call is needed.

## Routes

- `GET /v1/integrations/jira` Returns the safe Jira connection status after owner authorization.

## Adding a route

Keep this status endpoint a read: do not add write-license checks, audits, or credential changes. Add other Jira
connection endpoints in their owning route module, use `route_deps.context(request)` for app-scoped dependencies,
and add characterization cases before changing a handler or registration path.
