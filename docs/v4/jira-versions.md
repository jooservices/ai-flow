# Jira project versions

The control plane mirrors Jira project versions and each ticket's Fix versions during every Jira sync. Version IDs
from Jira remain stable registry keys; the registry refreshes each version's name, released state, and release date
for configured active projects. Every ticket returned by Jira replaces its complete set of ticket-version links,
so adding or removing a Fix version appears on the next incremental sync.

`GET /v1/projects/{key}/versions` returns the project's mirrored versions. Each version includes `id`, `name`,
`released`, `release_date`, `ticket_count`, `done_count`, and `open_tickets`. `open_tickets` lists the linked
tickets whose registry status is not `Done`, with each ticket's `key`, `summary`, and `status`. Tickets with an
empty Fix versions field have no links and do not contribute to any version's counts or open-ticket list.
