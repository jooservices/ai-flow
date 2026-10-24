# Work board

**Assumes:** you are signed in with any role (the Work board is open to every role and shows only your projects), and at least one project exists.

The Work board (`/work`) shows the tickets of one project by state, like [Delivery](delivery.md), as the working
view for the people who follow tickets day to day.

## What you see

- Pick the project at the top; the tiles show its tickets by state.
- Search by key or title, and filter by ticket type or epic.
- Open a ticket card for its runs, events and links to Jira. The card shows the pull request, the merge commit and the time between approval and merge. A ticket that is **Ready to merge** stays in the Human review column until the flow merges it (see `docs/v4/merged-means-done.md`). A ticket sent to **In Verify** because its work was already on the branch shows "Already done · sent to verification" and a link to its pull request (see `docs/v4/already-completed-work.md`).

## Page addresses

Every console page opens at its own address (for example `/dashboard`, `/work`, `/lead`), and `/cc` opens the
Dashboard. Reloading the browser keeps the page you were on. Without a session, each address sends you to `/login`.
New pages need no change to the proxy rule: only `/o2` and `/gatus` are kept away from the control API.

## Common tasks

- Open an epic to see only its tickets.
- A ticket that failed three runs in a row has a `ticket_stuck` incident: see its [runbook](../runbooks/ticket_stuck.md).
