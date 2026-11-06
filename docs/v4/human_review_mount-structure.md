# Human review mount structure

## Routes

`GET /v1/reviews` returns the project-scoped human review queue, its metrics and recent question threads. Its route
handler keeps authentication and the access-denied audit call at the HTTP boundary.

## Steps

`install_human_reviews_route` registers the route. `HumanReviewsEndpoint.human_reviews` checks the viewer's project
scope and delegates queue assembly to `_human_reviews_response`, which returns the ticket list, thread list and
metrics together.

`_review_context` loads selected keys, metrics, tickets, blockers and the configured review preview epic status. `_review_runs` loads
the latest development, overall, verification and applicable triage records. `_thread_filters` builds the event and
run filters for the selected project or role scope; `_review_thread_context` loads and orders the 50 most recent
threads, including tickets outside the active queue.

`_review_ticket_rows` preserves ticket order while `_review_ticket` builds each response. `_ticket_repository`
chooses the run or project repository and resolves a parent epic. `_pull_request_detail` loads a matched pull request
and logs provider failures. `_ticket_preview` exposes a preview only for a ticket in the configured review preview
project after its epic is Done;
`_review_actions` applies the item-kind actions and removes pull-request or skip-gate actions when their conditions
are absent. `_review_thread_rows` adds ticket details to recent threads that have messages.

To add another route to this mount, give it a named installer and register it explicitly from
`ReviewQueueAPI.mount_human_reviews` in the required route order. Keep route authorization and audit calls visible in
the endpoint, and put response assembly in small named helpers.
