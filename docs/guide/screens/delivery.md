# Delivery

**Assumes:** you are signed in as an Administrator, Product owner, Delivery or Team Lead, and at least one project exists.

Delivery (`/delivery`) shows one project at a time.

## What you see

- Pick the project at the top; the summary tiles show its tickets by state.
- **Board**: mirrors Jira every 20 seconds. Cards move as workers claim, finish and verify. A ticket that is **Ready to merge** stays in the Human review column until the flow merges it (see `docs/v4/merged-means-done.md`).
- **Needs attention**: tickets waiting on a person, older than 30 minutes, or whose last run did not finish. It also lists tickets that have waited more than 30 minutes for verification, with the accounts that tried and the ones not offered the ticket again (see `docs/v4/verify-no-verdict.md`).
- **Recent runs** of the project.
- Epic progress above the board; open an epic to see its tickets.

## Common tasks

- Open a ticket card for its timeline: runs, events and the writes AI Flow made to Jira.
- A ticket that failed three runs in a row has a `ticket_stuck` incident: see its [runbook](../runbooks/ticket_stuck.md).
