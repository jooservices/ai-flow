# Repeated run handoff

**Console screens:** `cc`, `machines`

Developer and verification outcomes are recorded by `record_run_outcome` in the existing
`RunFinished` listener. It saves an allow-listed diagnostic on the run row, counts the latest
consecutive unsuccessful outcomes across both steps, and opens one `ticket_stuck` incident on
the third failure. Candidate filtering and the atomic run claim both enforce the durable hold.
The coordinator resolves it from the incident drawer, where the explicit action comments in Jira,
removes the existing retry-hold label, moves the ticket to To Do, closes the incident, and resets the
streak. A watchdog retry or cooldown cannot clear the hold.

The incident record distinguishes evidence from unavailable measurements and links to the retained
support log when one exists. The Command Center shows run timing, node and model, usage, the
notification destination state, and the resolution control. If no enabled `Ticket needs a person`
route exists, the incident remains blocked and names that configuration gap.

## Routes

- `POST /v1/runs/{run_id}/result` records a node's run result, stores the run's diagnostic record and counts the run in the ticket's streak of unsuccessful runs.
- `POST /v1/runs/{run_id}/heartbeat` keeps a running run alive; when its lease was lost the run ends as lost, which also counts toward the streak.
- `POST /v1/runs/{run_id}/release` returns a run to the queue and publishes its end, so a released run is counted too.
- `POST /v1/master/actions` lets the master end a stuck run; the run's end is published and counted like any other.
- `POST /v1/nodes/{name}/heartbeat` stores the node's state together with one bounded CPU, load and memory sample (kept 30 days) that the diagnostic record uses as the nearest heartbeat.
- `POST /v1/work/claim` refuses a ticket whose hand-off is blocked or whose last three developer or verification runs all ended unsuccessfully.
- `POST /v1/version-releases/claim` applies the same refusal to version-release work.

## Stored data

`runs.details.diagnostic` is the structured record keyed by the existing ticket and run IDs. It
uses values from the run registry, progress heartbeat, result details and the nearest earlier node
resource sample. Missing measurements are stored as `unavailable`; free text is sanitized and
omitted when the secret scanner still finds sensitive material. Existing support logs remain in
their current sanitized 24-hour store.

`ticket_handoffs` has one row per ticket, a unique foreign key to its incident, and a checked `blocked` or
`resolved` state. `ticket_key` is the primary key so block checks by ticket are indexed; the unique incident
key serves coordinator lookup by incident ID. Resolution
time, actor and note retain the coordinator reset point. `node_heartbeat_samples` stores only CPU percentage,
1-minute load, CPU count and memory counters. Its `(node, heartbeat_at DESC, id DESC)` index serves the nearest
sample-at-or-before-outcome lookup; a second `heartbeat_at` index serves retention deletes. Samples older than
30 days are deleted during heartbeat writes.
At one sample every 15 seconds, the table grows by about 2,880 rows per node per day before
retention; each row contains only five numeric fields.

Outcome writes update `runs.details` and upsert the open `(kind, subject)` incident, then insert or
update the one `ticket_handoffs` row. Resolution writes its state, actor, note and timestamp before
closing that incident. Heartbeats insert one resource sample and delete expired samples. Reads use
the run primary key for diagnostics, the node/time index for the nearest sample, the ticket primary
key for candidate and claim checks, and the unique incident key for the resolution drawer.
`runs.details.diagnostic` follows the registry's existing run retention and `ticket_handoffs` keeps
one current state per ticket; expired node samples are removed by the 30-day delete.

The migration is additive: the previous release continues to read and write its existing run,
incident and node tables. New code writes the two new tables and the diagnostic JSON field; no
backfill or destructive schema change is needed. Deploy the migration before enabling the new
control release. Normal application database privileges are sufficient for both tables and
indexes.

## Adding a handoff diagnostic

Keep the outcome hook in the registered work listener, persistence in the incidents repository,
and orchestration in the incidents service. Add only allow-listed, sanitized values to the record;
update the incident drawer and its small renderer when adding a displayed field. Any new durable
lookup belongs in the repository with an index justified by its query. Keep the resolution route
thin and preserve the explicit coordinator action as the only way to clear a blocked handoff.

The console uses `GET /v1/runs/{run_id}/diagnostic-log` for retained logs and
`POST /v1/incidents/{incident_id}/resolve-and-requeue` for the coordinator action.
