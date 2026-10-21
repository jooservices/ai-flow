# AI Flow v4 Command Center: node registration and last seen

A node is registered by `/v1/nodes/join`; the IP it reports at join is stored with the node and is not
overwritten by a later join that leaves the IP empty. The startup sync of the control repo does not replace
that reported IP either, so an API restart keeps the address where the node was last seen. Each heartbeat
updates `last_heartbeat`.

`GET /v1/state` returns every registered node, including nodes that stopped reporting. The node fields
operators need are:

- `ip`: the address reported at join
- `joined_at`: when the node first registered
- `last_heartbeat`: ISO 8601 time of the last heartbeat, or `null` if it never sent one
- `heartbeat_age_s`: seconds since that heartbeat
- `metrics.hostname`: the hostname carried by the heartbeat

A node is shown as offline when its heartbeat is older than 75 seconds, without removing it from the list.
The node drawer shows the IP, joined time, hostname and last heartbeat. An offline node shows
`last seen <time> (<age>)`, so operators can tell when and where it was last alive. The watchdog opens a
`node_down` incident while the node is offline and closes it on the next heartbeat.

## Project closing report

Delivery's **Report** button opens the current project's Markdown closing report. If an epic is selected, the
report covers its descendant tickets. Use **Copy Markdown** to paste the report into the project's study case.
The API also returns the structured report at `GET /v1/projects/{key}/report`; add `?epic=KEY` to scope it to
an epic or `?format=md` for Markdown.

The period starts at the oldest ticket creation time in scope and ends at the latest completion time. Lead time
is measured from Jira creation to completion. `done_at` records when the control plane first observes a ticket
entering Done; existing Done tickets are backfilled from their current Jira update timestamp. Median and p90 lead
times are calculated across completed tickets with both timestamps. AI time sums recorded run durations per
completed ticket, then reports median and p90 over completed tickets with run history. Usage and run outcomes
include all recorded runs for tickets in scope. Rework rounds count verify runs with outcome `rework`; tickets
that needed the owner are taken from audited transitions to Needs owner, the current Needs owner status, or the
`needs-owner` label.
Incidents are included when their subject is a ticket in scope, so account-wide and node-wide incidents are not
assigned to an individual project.

## Delivery sorting and remembered filters

Delivery sorts epics by the numeric part of their key, so `TESTFLOW-2` comes before `TESTFLOW-10` instead of
after it. The same order is used in the epic picker and the Epics panel.

The selected project tab, type filter and epic are kept in `localStorage` under `aiflow4-delivery` and restored
on the next visit. Storage access is wrapped in `try`/`catch`, so a browser with storage disabled keeps working
with the defaults (first project, all types and all epics). A remembered project that no longer exists falls
back to the first project, and a remembered epic or type that the current project does not contain is reset to
`all`; the corrected selection is saved so the board never stays filtered to zero tickets.
