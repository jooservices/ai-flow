# Repeated flow failures

Each master cycle uses a fenced control API action to check completed `failed` and `timeout` runs in project `TESTFLOW`. It groups failures by
error kind and a normalized summary that removes run-specific ticket keys, IDs, paths, and numbers. When a pattern
has at least three runs across at least two tickets in a rolling 30-minute window, the master cycle creates one Jira
Bug in Intake with the `flow-bug` label. The description records the pattern, affected tickets, run IDs, steps, nodes,
node release versions, and a suggested v4 code location.

The control plane tracks each signature and its reported run IDs. Further runs for an open Bug add a comment with the
new run IDs and the repeat count; an unchanged snapshot does not add another comment. Once the Bug is resolved, a
later recurrence can create a new Bug. Separately, a `ticket_stuck` incident on a TESTFLOW ticket can trigger a Bug
when the incident has remained open for more than 30 minutes and its latest failure has an eligible signature.

Quota and authentication failures, lost runs, offline-node failures, and missing-tool symptoms do not become
`flow-bug` tickets. Their existing incidents remain the source of follow-up. Jira creation, transitions, and repeat
comments run inside the control API only after it validates the current master run's fencing token, and each write
is audited. An eligible `flow-bug` ticket is placed ahead of other tickets in the candidate list.
