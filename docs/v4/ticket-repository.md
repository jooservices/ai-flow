# Ticket repository

**Console screens:** Work board (`work`)

`ApplicationContext.ticket_repository` is the control API's single access point for the ticket mirror. The context
creates a `TicketRepository` from its database once; routes, services, listeners and loop helpers call named methods
on that shared object. `FakeTicketRepository` implements the same named-query methods in memory and can replace the
real repository on an application context in tests.

The repository keeps the existing `get`, `update_status`, `remove_label` and `update_description` operations, plus
one named method for each ticket query moved out of its callers. Query methods preserve the original SQL, parameter
order and database operation (`one`, `all`, `run` or `execute`).

To add a ticket query, add a descriptive method to `features/tickets/repository.py`, keep its SQL and row mapping
there, and add the same method to the fake or the fake's named-query responses. Test the real method with the database
fixture and the fake with an in-memory ticket row. Call it through `ctx.ticket_repository`; do not construct a
repository in the caller.

## Routes

These routes read or write tickets through the ticket repository (`ctx.ticket_repository`); their
behaviour is unchanged.

- `GET /v1/analytics/usage`
- `GET /v1/ba-chat/candidate`
- `GET /v1/business/{key}/change-requests`
- `GET /v1/business/{key}/release-progress`
- `GET /v1/business/{key}/release-readiness`
- `GET /v1/capacity`
- `GET /v1/dashboard/disagreements`
- `GET /v1/deploy/state`
- `GET /v1/integrations/jira/oauth/callback`
- `GET /v1/licensing/usage`
- `GET /v1/licensing/usage.csv`
- `GET /v1/master/autonomy`
- `GET /v1/master/candidate`
- `GET /v1/master/report`
- `GET /v1/projects`
- `GET /v1/projects/{key}/report`
- `GET /v1/projects/{key}/versions`
- `GET /v1/reviews`
- `GET /v1/stakeholder/{key}`
- `GET /v1/state`
- `GET /v1/tickets/{key}`
- `GET /v1/tickets/{key}/timeline`
- `GET /v1/version-releases/candidates`
- `GET /v1/work/candidates`
- `POST /v1/business/{key}/change-requests`
- `POST /v1/escalations/raise`
- `POST /v1/escalations/triage`
- `POST /v1/integrations/jira/api-token`
- `POST /v1/jira/{key}/description`
- `POST /v1/jira/{key}/fields`
- `POST /v1/jira/{key}/transition`
- `POST /v1/master/actions`
- `POST /v1/master/plan-outcome`
- `POST /v1/nodes/{name}/heartbeat`
- `POST /v1/nodes/{name}/release-failures`
- `POST /v1/nodes/{name}/worktrees/sweep`
- `POST /v1/notifications/channels/{channel_id}/test`
- `POST /v1/projects/{key}/close`
- `POST /v1/reviews/{key}/approve`
- `POST /v1/reviews/{key}/reject`
- `POST /v1/runs/{run_id}/result`
- `POST /v1/scale-proof/results`
- `POST /v1/stakeholder/{key}/verdict`
- `POST /v1/support/bundles`
- `POST /v1/system-check/run`
- `POST /v1/version-releases/claim`
- `POST /v1/version-releases/{version_id}/complete`
- `POST /v1/work/claim`
- `PUT /v1/role-health/settings`
- `its screen`
