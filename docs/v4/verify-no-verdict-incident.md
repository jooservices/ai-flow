# Verification exhaustion incident

When every eligible account is barred from verifying a ticket (two consecutive no-verdict results each), the watchdog
opens one `verify_no_verdict` incident that names the ticket and the reason. The shared incident detail does not list
account IDs. Repeated checks do not open a second incident for the same ticket. A temporary capacity loss keeps the
incident open; it closes when the ticket leaves verification or an unbarred eligible account can receive it. Idle-flow
detection skips tickets covered by an open `verify_no_verdict` incident so one stalled verification does not also raise
`flow_idle`.

## Steps

1. `ensure_offers` installs a verify candidate probe usable from control-loops without FastAPI.
2. `exhaustion_state` loads ticket history, builds the eligible account pool with `verify_retry_guard=False`, and
   returns which of those accounts are barred.
3. `check_incidents` opens one warning per exhausted ticket and closes incidents that have recovered or left
   verification.

To change the policy, update those named steps together and keep
`tests/v4/test_verify_no_verdict_incident.py` aligned. See the [verify_no_verdict runbook](runbooks/verify_no_verdict.md).

## Routes

No new HTTP route. The watchdog check runs from the control loop; open incidents appear in the existing incidents list
on Command Center (`GET /v1/state`).
