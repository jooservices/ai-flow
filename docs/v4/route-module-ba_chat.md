# BA chat routes

These routes are served from `control/routes/ba_chat.py` (a router found by the router registry), no longer from `api.py`.
Paths, request bodies, answers, status codes and permissions are unchanged:

- `GET /v1/ba-chat/candidate`
- `POST /v1/ba-chat/{job_id}/result`

They use the shared helpers of [route dependencies](route-dependencies.md). Their paths are not changed here; REST findings are
handled by JAIF-413.
