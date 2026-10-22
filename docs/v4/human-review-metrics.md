# Human review metrics and actions

The Human review console view combines the waiting queue, review actions, and project level metrics. The project
filter applies to the queue and metrics. **Export metrics CSV** downloads the displayed project scope.

## Metric definitions

All reporting uses UTC calendar weeks that start on Monday. The weekly chart shows the current week and the eight
preceding weeks. “Eight weeks ago” is the calendar week eight weeks before the current week.

- **People needed** counts transitions into `Human review` or `Needs owner` recorded in Jira transition events or
  accepted Jira transition audits. A current waiting ticket without recorded history is counted once using its Jira
  update time, or its registry sync time when Jira has no update time. A current ticket with an unresolved
  `blocked_by` dependency is counted as blocked using the same fallback timestamp. Asking AI to answer a question
  creates a `Needs owner` handoff for the triage worker; that handoff is excluded because it does not wait on a
  person.
- **Reason split** maps `Human review` to **Review before merge**. `Needs owner` with the `owner-escalated` label
  maps to **Decision needed**; otherwise a latest blocked development run maps to **Question from AI**, with other
  `Needs owner` items mapped to **Decision needed**. An unresolved `blocked_by` dependency outside those statuses
  maps to **Blocked**. A blocker is unresolved while its mirrored Jira status is not `Done` or `Closed`; a missing
  blocker mirror is treated as unresolved.
- **Finished with no person** is the share of tickets that reached `Done` in the prior eight calendar weeks and
  have no recorded transition into `Human review` or `Needs owner`, reviewer action audit, reviewer console run,
  or current `require-human` label.
- **Median person wait** pairs each transition into `Human review` or `Needs owner` with the next transition out.
  It uses episodes that started in the prior eight calendar weeks and includes a still-open wait through the
  current time.
- **Approved first time** is the share of decided review tickets whose first recorded review decision was approve.
  **Sent back or rejected** is the share of decided review tickets with any rework or rejection decision. The
  denominator is tickets with an approve, rework, or reject action. Older reviewer runs are used where action
  audits predate this feature.

The registry did not retain a complete status history before transition events and audits were introduced. For
older data, the current labels and mirrored state provide the best available reason classification; the definitions
above avoid presenting that inferred history as exact.

The suggestions beside the chart are starting points: clarify repeat AI questions in the ticket, move recurring
owner choices into project policy, narrow unnecessary merge gates, and make dependency ownership explicit.

## Waiting item actions

Actions use the configured reviewer bearer token, claim a short lived fenced QA run, and create a `human-review`
audit record. Jira comments, labels, and transitions use the same fence and have their own write audit rows.
Actions also comment on the linked pull request; approve and skip gate merge it, while reject closes it.

- **Review before merge** offers approve and merge, reject with a required reason, send for rework with a required
  comment, ask AI a question, and skip the `require-human` gate when that label is present.
- Reject closes the pull request, adds a `rejected` label, and moves the Jira ticket to `Done`.
- **Question from AI** and **Decision needed** offer an answer or an AI question. A human answer is added to Jira
  and the PR, clears `owner-escalated` when present, and returns the ticket to `To Do`. An AI question is added to
  Jira and the PR, clears owner escalation, and makes the ticket eligible for the existing triage worker. The
  worker answer appears in the thread, is mirrored to the PR when one is linked, and returns the ticket to
  `Human review` so the reviewer can continue without starting another development run.
- **Blocked** offers an AI question so the owner can ask for analysis of the blocked work.

Waiting items can exist before a pull request has been created. In that case the action is still fenced and written
to Jira and the audit log; there is no PR conversation to update. Actions that merge or reject a change require a
linked pull request.

Recent question threads stay visible in the Human review view after an answered ticket returns to `To Do`.
