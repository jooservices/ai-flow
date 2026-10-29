# Human review queue

The console's **Human review** tab lists Jira tickets mirrored in the `Human review` status. Each card shows the
ticket summary and description, Jira link, GitHub pull request, GitHub diff totals, and the latest verify outcome,
checks, and findings. GitHub diff totals are left blank when the PR details cannot be read.

## Preview link

For project `TESTFLOW`, the queue reads the preview URL from the project's control repo configuration:

```yaml
environments:
  preview:
    url: https://preview.example.test
```

The queue only includes this link after Jira epic `TESTFLOW-5` is mirrored as `Done`. A missing epic, incomplete
epic, or missing preview URL keeps the link hidden.

## Notifications

When a ticket with label `require-human` enters `Human review`, either through the fenced Jira transition API or a
Jira status change observed by `jira_sync`, notify sends one review alert. Email goes to the Jira assignee's email
address when Jira supplies it; otherwise it uses `AIFLOW4_ALERT_EMAIL` (default `owner@aiflow4.local`). Other
configured notify channels continue to receive the alert through the shared notify interface. An initial sync of a
ticket already in `Human review` does not count as a detected transition.

## Reviewer actions

The control API requires a dedicated reviewer bearer token before it will create a fenced review run. Configure
`AIFLOW4_REVIEWER_TOKEN` in the control API's existing secret environment file. If no token is configured, review
actions fail closed with HTTP 503; missing or incorrect tokens receive HTTP 401. Enter the token in the password
field on the Human review tab. The console keeps it in page memory and sends it only in the `Authorization`
header for reviewer actions. Metrics, action availability, and the complete action list are documented in
[Human review metrics and actions](human-review-metrics.md).

**Approve and merge** acquires a ticket lease, creates a short lived QA run with a fencing token, and runs the
same GitHub merge operation used by verify. When GitHub confirms the merge, control records a Jira comment and
moves the ticket to `Done`. The API action is `POST /v1/reviews/{key}/approve`; `POST /v1/reviews/{key}/skip-gate`
uses the same merge path when the `require-human` gate is present. After a confirmed merge, the run is captured
and only that newly closed ticket is considered for a study case. If an approval merge is refused, the flow
comments on the PR and moves the ticket to `Ready to merge` so it can retry; the ticket remains in `Human review`
only if that Jira transition fails. A refused skip-gate merge leaves the ticket in review.

**Send for rework** requires a comment, records it on the Jira ticket, adds the `rework` label, and moves the ticket
to `To Do`. Other reviewer actions use `POST /v1/reviews/{key}/answer`, `POST /v1/reviews/{key}/ask-ai`,
`POST /v1/reviews/{key}/reject`, and `POST /v1/reviews/{key}/rework`. Reviewer actions are recorded in
`write_audit` with their run fence; these actions do not trigger a closed-ticket study-case backfill.
