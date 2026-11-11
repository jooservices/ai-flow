# Completed-ticket notes in claim context

Automatically captured completed-ticket notes remain stored and visible in the
authorized Knowledge screen and API. They are omitted from database knowledge
packs sent with `dev`, `verify`, and `qa` claims, so those notes cannot crowd out
other approved, visible guidance. The existing scope, approval, and retirement
rules continue to apply.

Claims for other steps keep their existing knowledge selection. To extend the
exclusion to another claim step, add its step value to the completed-ticket
step set in `features/work/claim_extras.py`.

## Routes

- `GET /v1/knowledge/items` Returns the authorized Knowledge dashboard and item list for the requested project and query.
- `POST /v1/work/claim` Claims work for the authenticated node and returns the run payload with its selected context.
