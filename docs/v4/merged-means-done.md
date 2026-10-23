# Merged means Done

When a ticket has passed review and QA its pull request is merged and the ticket moves to Done. Whoever merges a pull
request, Jira follows GitHub within a minute. Code: `aiflow/v4/control/merge_flow.py`, run by the control loops.

## The merge step (every 30 seconds)

A ticket is in the merge queue when its Jira status is **Ready to merge**, or it was approved in Human review and is
not merged yet. For each one the flow, under the ticket lease so it never races with a run:

1. reads the pull request; a pull request already merged or closed is handled by the reconcile rules below;
2. **branch conflicts** (`mergeable_state` dirty, or GitHub reports a conflict while updating the branch): the ticket
   goes to **To Do** with the `rework` label. The conflict is the reason on the Jira ticket, on the pull request and
   in the findings the next dev run receives;
3. **branch behind the base**: updates the branch on GitHub and waits for the checks on the new head;
4. **checks**: pending checks are awaited (the ticket shows "waiting: the checks are still running"); a failing check
   sends the ticket to development with the failing checks as the reason;
5. merges (the project's `merge.method`, squash by default) and moves the ticket to **Done**.

Approving in Human review merges at once when the pull request can merge. When it cannot yet, the ticket becomes
**Ready to merge** (it stays in Human review if the Jira workflow has no such status, with the approval kept) and this
step merges it later. Nobody has to press merge.

On the Work board and the Dashboard flow, **Ready to merge** tickets stay in the Human review column (the pill shows
the exact status).

## The reconcile loop (every 60 seconds, and on every webhook)

Every ticket that has a pull request is compared with GitHub. Only a *change* of the pull request is acted on, so a
ticket that a person moved on purpose is not pushed back every minute:

| GitHub | Ticket |
|---|---|
| merged | **Done**, with the merge time (`done_at` is the merge time), commit and who merged |
| closed without merge | **To Do**, with the reason on the ticket and in the next dev run's findings |
| reopened | **In Verify** |

A merge by the flow or by a person reaches Jira within one minute of the merge (the loop period). History is not
rewritten: on the first look at a pull request, a Done ticket with a closed pull request, or a To Do/Backlog/Intake
ticket with a merged one, is only recorded. A ticket rejected in Human review is final even though its pull request is
closed.

**Webhook (optional).** Set `AIFLOW4_GITHUB_WEBHOOK_SECRET` (environment or secret file) and point a GitHub webhook
with the `pull_request` event at `POST /v1/github/webhook`. The signature (`X-Hub-Signature-256`) is checked; the
pull request is reconciled at once and the merge step runs. Without the secret the route answers 404 and the
one-minute loop works alone.

## What the ticket shows

The ticket drawer (Work board, Dashboard) has a **Pull request and merge** section: the pull request link, its state
on GitHub, when it was approved and merged, the merge commit, the time between approval and merge, any note such as
"waiting: the checks are still running", and the **moves and their cause**. The same data is returned by
`GET /v1/tickets/{key}` as `merge` and `moves`.

## Cause of every move

Each move is stored in `ticket_moves` (and as a `ticket.moved` event on the timeline) with one of:

- `flow merge`: the merge step, or the verify step's own merge;
- `person`: a console review action (approve, rework, reject, skip gate) or a move made in Jira (seen by the Jira sync);
- `reconcile`: the reconcile loop following GitHub;
- `flow`: any other step of the flow (dev, verify rework, intake, ...).

## Disagreements on the Dashboard

If the flow cannot fix a ticket (for example Jira refuses the transition) the ticket is kept in
`ticket_disagreements` and retried every minute. The Dashboard block **Jira and GitHub disagree** lists the tickets
that disagreed for more than five minutes: the Jira status, the state of the pull request, the status the flow will
set, how long, and why it is not fixed yet. The list is empty when everything matches. API: `GET
/v1/dashboard/disagreements`.
