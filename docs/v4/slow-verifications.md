# Slow verifications on the Dashboard

Tickets can sit in verification for a long time when runs end without a verdict or no verifier claims them. The
Dashboard lists those waits so an owner can see which tickets are stuck and which accounts already tried.

**Console screens:** `cc`, Delivery (`delivery`), Work board (`work`)

## Routes

- `GET /v1/state` Returns the console snapshot, including `slow_verifications` and per-ticket `slow_verify` lines,
  filtered by the signed-in user's project scope and machine-access permission.
- `GET /v1/deploy/state` Returns the deployment registry snapshot after checking the deployment token.
- `GET /v1/services` Returns the service catalogue used by the console Help and status pages.
- `GET /v1/tickets/{key}` Returns one ticket drawer payload (ticket, runs, events and audited writes).
- `GET /v1/console/layouts` Returns the signed-in person's saved console layouts and role defaults.
- `PUT /v1/console/layouts/{page}` Saves the signed-in person's layout for one console page.
- `DELETE /v1/console/layouts/{page}` Clears the signed-in person's saved layout for one console page.

## Steps

1. `waiting_verifications` selects tickets in `In Verify` that are not epics, not held, have no running run, and have
   waited more than 30 minutes since the latest successful developer run ended (or since Jira sync times when none
   exists). The threshold is exclusive: exactly 30 minutes does not appear.
2. `slow_verifications` loads the current development-round verify history for those tickets and reports each account
   that tried, how many of those runs ended without a verdict, and whether the account is barred.
3. `attach_slow_verifications` sets `tickets[].slow_verify` to a one-line description and returns the list for
   `/v1/state`. Callers without `view.machines_ai` see `Verifier N` labels instead of account ids.
4. The Dashboard Work tab renders the `slow-verifications` block from `slow_verifications`. Delivery "Needs attention"
   also surfaces a waiting ticket through `tickets[].slow_verify`.

To change the wait threshold or the account summary, update the repository query and `describe` together, then keep
`tests/v4/test_slow_verifications.py` aligned.
