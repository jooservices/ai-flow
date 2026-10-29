# Analytics routes

These routes are served from `control/routes/analytics.py` (a router found by the router registry), no longer from `api.py`.
Paths, request bodies, answers, status codes and permissions are unchanged:

- `GET /v1/analytics/conflicts`
- `GET /v1/analytics/daily`
- `GET /v1/analytics/usage`
- `GET /v1/analytics/usage/daily`

They use the shared helpers of [route dependencies](route-dependencies.md). Their paths are not changed here; REST findings are
handled by JAIF-413.
