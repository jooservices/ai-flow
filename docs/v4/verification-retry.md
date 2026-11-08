# Verification retry after a missing verdict

Verification workers keep the existing failed outcome and summary when the response has no `VERDICT:` line. They add
`details.no_verdict = true` so control can distinguish this result from an ordinary verification failure.

## Routes

- `GET /v1/deploy/state` Returns the deployment registry snapshot after checking the deployment token.
- `GET /v1/role-health` Returns the account and role health summary used by the console.
- `GET /v1/state` Returns the console state snapshot, filtered by the signed-in user's permissions.
- `POST /v1/runs/{run_id}/result` Records a node's fenced run result and runs follow-up health evaluation.
- `PUT /v1/role-health/settings` Updates the role health failure window and threshold, then reevaluates role health.

The retry path has three named steps:

1. `collect_verification` marks a missing verdict on the stored run result.
2. `verify_retry_backoff` and `verify_ticket_history` in `repositories/runs.py` load ordinary retry delays and the
   ticket's verifier history, including when the latest no-verdict run ended. No-verdict rows bypass the ordinary
   verify delay.
3. `verify_candidates` applies `_verify_retry_blocked` and `_untried_verify_peer`: an eligible account that has not
   ended this ticket without a verdict gets the next attempt. Peer eligibility is loaded once per account and node
   during a poll and reused for each ticket. If an eligible peer does not claim the ticket, the prior verifier can
   retry 15 minutes after its no-verdict run ends. Two consecutive no-verdict results bar that account from the ticket.
   Role health excludes the first no-verdict result and scores the second and later consecutive ones.

To change this policy, update the repository queries and candidate rule together, then keep the account-ordering,
streak, and ordinary-failure tests in `tests/v4/test_verify_no_verdict.py` aligned. The retry history is scoped to the
ticket and account and includes earlier development rounds.
