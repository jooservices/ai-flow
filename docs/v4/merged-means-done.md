# Merged means Done

**Console screens:** `cc`, `work`, `delivery`

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
5. merges (the project's `merge.method`, squash by default) and moves the ticket to **Done**. The merge request is
   **pinned to the commit whose checks were read** (the `sha` of GitHub's merge endpoint): when the branch moved on
   in between, GitHub refuses (409), the ticket shows "the branch changed after its checks were read" and the next
   pass reads the new head and its checks. The flow never merges a commit it did not check.

The merge time stored on the ticket is the merge itself: GitHub's `merged_at`, or the clock right after the merge call
when GitHub cannot be read. It is never the time of the Jira move that follows, so a slow Jira does not lengthen the
time between approval and merge.

Approving in Human review merges at once when the pull request can merge. When it cannot yet, the ticket becomes
**Ready to merge** (it stays in Human review if the Jira workflow has no such status, with the approval kept) and this
step merges it later. Nobody has to press merge.

On the Work board, Delivery and the Dashboard flow, **Ready to merge** tickets stay in the Human review column (the
pill shows the exact status) and count as in flight.

## Leases

The control plane writes Jira for a ticket only under the same ticket lease and fence a worker uses. The merge step and
every reconcile move claim the lease (120 s) and renew it before each GitHub or Jira call; a lease that cannot be
renewed stops the step before the next write. When a run still owns the ticket, reconcile writes nothing, records the
reason ("a run holds the ticket, tried again next pass") and the next pass tries again, so the active worker's fence is
never overridden.

## The reconcile loop (a pass every 20 seconds, and on every webhook)

Every ticket that has a pull request is compared with GitHub, in a thread of its own so that the Jira sync, the
watchdog and the merge step can never delay it:

| GitHub | Ticket |
|---|---|
| merged | **Done** from whatever status Jira has (also To Do, Backlog, Intake, or a ticket moved out of Done later), with the merge time (`done_at` is the merge time), commit and who merged |
| closed without merge | **To Do**, with the reason on the ticket and in the next dev run's findings |
| reopened | **In Verify** |

A closed pull request is acted on once (when it changes), so a ticket that a person moved on purpose after the close is
not pushed back every pass. A ticket rejected in Human review is final even though its pull request is closed. A merged
pull request is different: Jira must show Done, so a ticket outside Done is moved there until it is.

**The one-minute bound.** A merge by anyone reaches Jira within one minute (`RECONCILE_BOUND_S`). A pass starts every
20 s (`RECONCILE_EVERY_S`) and starts no new pull request after 20 s (`RECONCILE_BUDGET_S`); pull requests are read
four at a time, the ones checked longest ago first, and those a pass could not reach head the next one. A backlog is
therefore rotated, never starved, and every pull request is reached within 40 s plus the slowest call. A pull request
GitHub cannot answer for is marked as looked at so it does not hold the front of the queue.

**Webhook (optional).** Set `AIFLOW4_GITHUB_WEBHOOK_SECRET` (environment or secret file) and point a GitHub webhook
with the `pull_request` event at `POST /v1/github/webhook`. The signature (`X-Hub-Signature-256`) is checked; the
pull request is reconciled at once and the merge step runs. Without the secret the route answers 404 and the
loop works alone.

## What the ticket shows

The ticket drawer (Work board, Dashboard, Delivery) has a **Pull request and merge** section: the pull request link, its
state on GitHub, when it was approved and merged, the merge commit, the time between approval and merge, any note such
as "waiting: the checks are still running", and the **moves and their cause**. The same data is returned by
`GET /v1/tickets/{key}` as `merge` and `moves`.

## Cause of every move

Each move is stored in `ticket_moves` (and as a `ticket.moved` event on the timeline) with one of:

- `flow merge`: the merge step, or the verify step's own merge;
- `person`: a console review action (approve, rework, reject, skip gate) or a move made in Jira (seen by the Jira sync);
- `reconcile`: the reconcile loop following GitHub;
- `flow`: any other step of the flow (dev, verify rework, intake, ...), the watchdog's requeue of an orphaned or
  lease-lost In Progress ticket included (it writes the mirror first, so the Jira sync would see no change).

## Disagreements on the Dashboard

If the flow cannot fix a ticket (Jira refuses or ends at another status, or a run holds the ticket) the ticket is kept
in `ticket_disagreements` and retried every pass. The record is cleared only after the ticket is at the status GitHub
implies. The Dashboard block **Jira and GitHub disagree** lists the tickets that disagreed for more than five minutes:
the Jira status, the state of the pull request, the status the flow will set, how long, and why it is not fixed yet. The
list is empty when everything matches. API: `GET /v1/dashboard/disagreements`.

## Control API routes that take part

The merge and the move of a ticket are also made by these routes, which record the cause and the merge facts:
`POST /v1/github/merge` (the verify step's merge), `POST /v1/jira/{key}/transition` (a move made by a run),
`POST /v1/reviews/{key}/approve`, `POST /v1/reviews/{key}/reject`, `POST /v1/reviews/{key}/skip-gate`,
`POST /v1/reviews/{key}/answer`, `POST /v1/reviews/{key}/ask-ai`, `POST /v1/reviews/{key}/rework` (human review actions
are recorded as `person`), `GET /v1/reviews`, `GET /v1/reviews/export.csv`, `POST /v1/github/pr`,
`GET /v1/github/git/{path}`, `POST /v1/github/git/{path}` and `POST /v1/master/actions`.
