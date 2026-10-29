# Jira routes

These routes are served from `control/routes/jira.py` (a router found by the router registry), no longer from `api.py`.
Paths, request bodies, answers, status codes and permissions are unchanged:

- `POST /v1/jira/{key}/comment`
- `POST /v1/jira/{key}/description`
- `POST /v1/jira/{key}/fields`
- `POST /v1/jira/{key}/transition`

They use the shared helpers of [route dependencies](route-dependencies.md). Their paths are not changed here; REST findings are
handled by JAIF-413.
