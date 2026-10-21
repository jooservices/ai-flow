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

**Approve and merge** acquires a ticket lease, creates a short lived QA run with a fencing token, and runs the
same GitHub merge operation used by verify. When GitHub confirms the merge, control records a Jira comment and
moves the ticket to `Done`. If the merge is refused, the ticket stays in review.

**Send for rework** requires a comment, records it on the Jira ticket, adds the `rework` label, and moves the ticket
to `To Do`. Both actions are recorded in `write_audit` with their run fence.
