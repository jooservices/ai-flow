# Retry hold: the third unsuccessful run stops the retries

A ticket whose runs keep ending the same way is not offered again after the third time. `control/retry_hold.py` runs in
every watchdog pass.

- **Streak:** consecutive unsuccessful runs (`failed`, `timeout`, `lost`, `no_changes`) of one ticket and step (`dev`,
  `verify`), counted over the whole history since the last release (not only the last day). A run that produced work ends
  the streak. Each watchdog requeue of an In Progress ticket without a run (`To Do (orphan)`) adds one.
- **Hold:** at three, the ticket gets the Jira label `aiflow-hold` (work claims already skip that label) and one comment
  that lists the last three runs: run id, outcome and error kind, node, account, duration. One row in `retry_holds` records
  the hold; a Jira failure leaves no row, so the next watchdog pass tries again.
- **Release:** removing the label `aiflow-hold` is the explicit release. The streak starts again from zero, so one new
  failure does not hold the ticket again. A cooldown or a watchdog requeue alone never restarts a held ticket.
- **Diagnostics records and the Command Center view** are the other slices of JAIF-354.
