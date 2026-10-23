# Work board

**Assumes:** you are signed in with any console role and at least one project exists.

The Work board (`/work`) shows the tickets of one project by state, like [Delivery](delivery.md), as the working
view for the people who follow tickets day to day.

## What you see

- Pick the project at the top; the tiles show its tickets by state.
- Search by key or title, and filter by ticket type or epic.
- Open a ticket card for its runs, events and links to Jira.

## Page addresses

Console pages include `/dashboard`, `/work`, and `/lead`; `/cc` is an alias for the Dashboard. The Traefik
catch-all routes console pages and `/v1` requests to the control API, excluding `/o2` and `/gatus`, so new pages
need no individual path entry.

## Common tasks

- Open an epic to see only its tickets.
- A ticket that failed three runs in a row has a `ticket_stuck` incident: see its [runbook](../runbooks/ticket_stuck.md).
