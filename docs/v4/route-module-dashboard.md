# Dashboard read routes

The three Dashboard read models are served from `control/routes/dashboard.py`, no longer from `api.py`. Paths, answers and
permissions (`view.dashboard` when sign-in is on; the legacy open console otherwise) are unchanged:

- `GET /v1/dashboard/waiting` lists the Human review ticket keys the caller may see.
- `GET /v1/dashboard/disagreements` lists tickets whose Jira state and pull request disagreed for more than five minutes.
- `GET /v1/dashboard/quota` summarizes the quota windows of the accounts serving the caller's projects.

The review queue is shared with the router through `app.state.review_queue`, set when the API is created (see
[route dependencies](route-dependencies.md)). Paths are not changed here; REST findings are handled by JAIF-413.
