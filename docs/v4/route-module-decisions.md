# Owner decision routes

The owner decision register is served from `control/routes/decisions.py` (a router found by the router registry), no longer from
`api.py`. Paths, request bodies, answers, status codes and permissions are unchanged:

- `POST /v1/decisions` adds a decision (`console.write`; a pattern that is not a regular expression answers 400).
- `POST /v1/decisions/{decision_id}/answer` answers a waiting question with the recommended option or a chosen one (404 when
  nothing waits).
- `POST /v1/decisions/{decision_id}/retire` retires a decision (404 for an unknown one).

Each action writes an `escalation` row to the audit log through the shared helpers ([route dependencies](route-dependencies.md)).
Their paths are not changed here; REST findings are handled by the API conformance tickets (JAIF-413).
