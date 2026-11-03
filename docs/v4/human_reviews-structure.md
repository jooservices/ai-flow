# Human review queue structure

**Routes:** `GET /v1/reviews` in `ReviewQueueAPI.mount_human_reviews`.

The route keeps authentication, project-scope refusal and its access-denied audit in place. It then calls
`_build_human_reviews_response`, which loads the selected queue keys and metrics, ticket rows, thread rows, and finally
the ticket and recent-thread response sections in the existing order.

`HumanReviewRows` groups selected tickets with blocker statuses, the TESTFLOW epic status, the latest development,
overall and verify runs, and the latest ask and triage rows for Needs owner tickets. `_load_human_review_rows` calls
`_load_human_review_tickets` for selected tickets, blocker statuses and epic status, then
`_load_human_review_runs` for run and question rows. Run lookups are skipped when the queue is empty.

`_human_review_thread_filters` gives an explicit project filter precedence over role scope; otherwise it returns
project-array filters for the scope or empty filters for an unrestricted user. `_load_human_review_threads` combines
the latest ask-ai event date with the latest blocked, answered or escalated development and triage run date, keeps the
50 most recent tickets, loads their messages, and fetches ticket summaries for recent tickets outside the queue.
`HumanReviewThreads` carries the message map, ticket map and ordered recent keys between those reads and the response
builder.

`_human_review_repository_and_pr` uses the repository from the development run when both it and project configuration
exist. Otherwise it uses the configured repository, adding the epic returned by `ticket_epic` when the ticket has a
parent; without project configuration the repository is empty. It reads PR details only for a URL matching
`/pull/<number>` and logs the existing warning while leaving details empty if that read fails.

`_human_review_actions` starts from the item's existing action list, removes approve, reject and skip-gate without a
matching PR, and removes skip-gate for review items without `require-human`. `_human_review_preview_url` reads a string
preview or a dictionary's `url`, then exposes it only for TESTFLOW when epic `TESTFLOW-5` is Done.
`_build_human_review_ticket` combines those values with the live blocker check, item kind, thread, Jira URL, PR and
verify fields. `_build_recent_human_review_threads` includes only recent tickets that have both a ticket row and at
least one message.

To add a response section, add a named builder beside the existing section builders, call it directly from
`_build_human_reviews_response`, and extend `test_human_reviews_parity.py` with literal output and call-order coverage.
Keep route calls explicit so the documentation checker can follow the full response path.
