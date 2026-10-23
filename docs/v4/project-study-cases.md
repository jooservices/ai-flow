# Project close reports and study cases

The Delivery view can close a project with **Close project**. The control API also accepts
`POST /v1/projects/{key}/close` with an optional `{"actor":"owner"}` body. Closing is idempotent while the
project remains closed. The console suggests closure after every mirrored project ticket, including epics, is
Done; a lead or owner may close it earlier through the same control path.

When an epic first reaches Done, the Jira sync (or a fenced Jira transition) creates an epic-scoped report. A
project close creates a project-scoped report. Both use the report aggregator and include lead time, AI time,
tokens, estimated run cost, incidents, rework rounds, tickets that needed owner input, and lessons inferred from
recorded rework, owner escalations, and incidents. A clean recorded history is described as such; reports do not
invent unobserved lessons.

Each report is saved as an immutable row in `project_reports` and indexed in memory as `study_case`. Search these
entries through `GET /v1/memory/search?project={key}&q={words}`. The full Markdown is available to the Knowledge
screen at `GET /v1/projects/{key}/study-cases` or `GET /v1/memory/study-cases?project={key}`. Those endpoints
return the project, stable study-case reference, report text, and creation time. The Knowledge screen can consume
the same API when it is available.

The configured alert email channel receives the Markdown report at `AIFLOW4_ALERT_EMAIL`; other configured alert
channels receive the same notification using the existing notify routing. `sent_channels` on the close API result
and in `project_reports` records which channels accepted the notification.

The project lifecycle keeps a monotonically increasing close version. If Jira sync or the Business change request
form adds a ticket after closure, the project reopens and its next close stores a new study-case version without
replacing earlier reports. Epic report versions are tracked independently by epic.

The current control API uses the same trusted operator boundary as the existing console and business endpoints;
it does not have per-user login identities to distinguish leads from owners.
