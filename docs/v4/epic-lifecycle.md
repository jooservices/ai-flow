# Automatic epic lifecycle

## Routes

- `POST /v1/jira/{key}/transition` applies a worker's status change and records the move for its timeline and outbox reactions.
- `POST /v1/incidents/{incident_id}/resolve-and-requeue` records a coordinator resolution and requeues the held ticket so its parent epic can follow the move.

AI Flow checks every descendant of each mirrored epic, including children in another project and tickets nested below
another child. An epic moves to **In Progress** when a child starts work, and to **Done** when every descendant is
**Done** or **Declined**. If a child is newly seen under an existing epic, moved under the epic, or reopened after the
epic was Done, the epic moves back to **To Do**. A newly mirrored epic with an unfinished child stays in its Jira
status during initial import, and a child change from before a manual close does not undo that close. An epic with no
children is left unchanged.

The `epic_lifecycle` module runs from the `TicketMoved` listener after a child transition, and from Jira sync after it
mirrors external changes. The watchdog retries a failed transition using the child's Jira status-change or parent-change
time, so a failed reopen remains pending and a later manual close stays authoritative. It also retries start and
completion transitions from the current mirror. Each automatic epic move is also recorded with the `epic children`
cause. To add or change a status rule, update `target_status` and its tests;
keep ticket queries in the tickets repository and periodic reconciliation in `control/watchdog.py`.

When Flow moves an epic to Done, it comments with the child count, UTC completion time and a link to the HTML
epic report. The comment separates Done and Declined children when a child was declined. If that completes the
project's last open epic, the comment also suggests that the lead consider closing the project and links to the
project report. Flow does not close the project.

Project and epic reports expose `project_close_suggestion` with `suggested`, `epics_done` and `epics_total`.
The JSON response and the Markdown and HTML exports say to consider project closure only when the project has at
least one epic and every epic is Done. This also covers projects whose last epic was moved to Done outside Flow.
