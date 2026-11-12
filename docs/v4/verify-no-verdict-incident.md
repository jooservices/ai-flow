# Verification exhaustion incident

When every eligible account has at least one completed verification without a `VERDICT:` line in the current
development round, the watchdog opens one `verify_no_verdict` incident that names the ticket and explains the missing
verdicts. The shared incident detail does not list account IDs. Repeated checks do not open a second incident for the
same ticket. This warning does not change the separate two-consecutive-result offer bar. A temporary capacity loss
keeps the incident open; it closes when the ticket leaves verification or an eligible account has no current-round
no-verdict result. Idle-flow detection skips tickets covered by an open `verify_no_verdict` incident so one stalled
verification does not also raise `flow_idle`.

## Steps

1. `ensure_offers` installs a verify candidate probe usable from control-loops without FastAPI.
2. `exhaustion_state` loads the existing current-round ticket history, builds the eligible account pool with
   `verify_retry_guard=False`, and reports exhaustion when each eligible account has any `no_verdict=true` result.
3. `check_incidents` opens one warning when the pool is nonempty and exhausted. It closes an incident when an
   eligible account has no such result or the ticket leaves verification, and preserves it while the pool is empty.

To change the exhaustion policy, update those named steps together and keep
`tests/v4/test_verify_no_verdict_incident.py` aligned. The offer bar remains in
[`verify-account-bar.md`](verify-account-bar.md). See the [verify_no_verdict runbook](runbooks/verify_no_verdict.md).

## Routes

No new HTTP route. The watchdog check runs from the control loop; open incidents appear in the existing incidents list
on Command Center (`GET /v1/state`).
