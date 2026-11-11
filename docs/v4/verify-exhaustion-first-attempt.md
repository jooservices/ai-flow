# Verification exhaustion after every eligible account tries

The watchdog reports a ticket when its nonempty eligible verifier pool has each recorded at least one completed
current-round result with `no_verdict=true`. The existing Command Center incident list shows the warning. It does not
change the separate policy that bars a verifier after two consecutive no-verdict results.

## Steps

1. `ensure_offers` installs the candidate probe used by the control loop.
2. `pool` reuses the verifier account, node and ticket eligibility checks with `verify_retry_guard=False`.
3. `exhaustion_state` uses `verify_ticket_history` to check each pool account against the current development round.
4. `check_incidents` opens one warning for a nonempty exhausted pool, preserves it when capacity is empty, and closes it
   when an eligible account has no current-round marker or the ticket leaves verification.

To change this report, update the named steps and the cases in `tests/v4/test_verify_no_verdict_incident.py` together.
Keep the history query and offer bar unchanged unless their separate policies are intentionally being changed.
