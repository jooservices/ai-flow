# Ticket repository

**Console screens:** `work`

`ApplicationContext.ticket_repository` is the control API's single access point for the ticket mirror. The context
creates a `TicketRepository` from its database once; routes, services, listeners and loop helpers call named methods
on that shared object. `FakeTicketRepository` implements the same named-query methods with in-memory ticket rows and
canned query responses, and can replace the real repository on an application context in tests.

The repository keeps the existing `get`, `update_status`, `remove_label` and `update_description` operations, plus
one named method for each ticket query moved out of its callers. Query methods preserve the original SQL, parameter
order and database operation (`one`, `all`, `run` or `execute`).

To add a ticket query, add a descriptive method to `features/tickets/repository.py` and register it in
`TicketRepository.NAMED_QUERIES`. The fake inherits that method and handles its result through in-memory state or a
named canned response. Cover the method in the parity test, test its SQL against the database fixture, and inject the
fake through `ApplicationContext.ticket_repository` when a test needs no database. Call it through the context; do not
construct a repository in the caller.

## Routes

None of these routes changes its answer. Each one reads or writes tickets only through the tickets repository now:

- `GET /v1/analytics/usage` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/ba-chat/candidate` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/business/{key}/change-requests` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/business/{key}/release-progress` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/business/{key}/release-readiness` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/capacity` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/dashboard/disagreements` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/deploy/state` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/integrations/jira/oauth/callback` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/licensing/usage` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/licensing/usage.csv` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/master/autonomy` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/master/candidate` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/master/report` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/projects` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/projects/{key}/report` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/projects/{key}/versions` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/reviews` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/stakeholder/{key}` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/state` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/tickets/{key}` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/tickets/{key}/timeline` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/version-releases/candidates` keeps its response; the ticket queries behind it moved into the tickets repository.
- `GET /v1/work/candidates` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/business/{key}/change-requests` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/escalations/raise` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/escalations/triage` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/incidents/{incident_id}/resolve-and-requeue` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/integrations/jira/api-token` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/jira/{key}/description` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/jira/{key}/fields` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/jira/{key}/transition` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/master/actions` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/master/plan-outcome` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/nodes/{name}/heartbeat` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/nodes/{name}/release-failures` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/nodes/{name}/worktrees/sweep` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/notifications/channels/{channel_id}/test` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/projects/{key}/close` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/reviews/{key}/approve` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/reviews/{key}/reject` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/runs/{run_id}/result` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/scale-proof/results` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/stakeholder/{key}/verdict` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/support/bundles` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/system-check/run` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/version-releases/claim` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/version-releases/{version_id}/complete` keeps its response; the ticket queries behind it moved into the tickets repository.
- `POST /v1/work/claim` keeps its response; the ticket queries behind it moved into the tickets repository.
- `PUT /v1/role-health/settings` keeps its response; the ticket queries behind it moved into the tickets repository.
- `its screen` keeps its response; the ticket queries behind it moved into the tickets repository.
