# Mechanical returns

**Console screens:** Command Center (`cc`)

The developer step refreshes a ticket branch with the latest base before it runs the configured local gate and opens a
draft pull request. The shared gate already runs the project's fast CI checks, including `docs-check`; when that check
names a missing documentation page, the developer gets one focused repair turn and the gate runs again. A verification
run refreshes its branch again if the base moves while the ticket waits.

## Steps

- `Git.merge_base_resolving` merges the configured base and resolves only unmerged paths whose mode and blob are
  identical on both sides.
- `mechanical.refresh_base` runs before developer hand-over. It refuses a real conflict with the conflicting paths in
  the run details and the developer note, then preserves the branch with a branch-scoped credential and the branch
  head recorded at checkout as its push lease.
- `dev.repair_documentation_gap` handles a named `docs-check` page failure with a documentation-only turn, rejects a
  repair that changes `HEAD` or touches paths beyond `docs/v4/*.md` and `deploy/v4/README.md`, commits the repair, and
  reruns the configured gate once. Run usage and quota metadata include both turns.
- `verify.prepare_verify` checks the fetched PR head against the current base, asks GitHub to update a stale branch,
  and does not start the reviewer while the remote branch is still behind.
- `GET /v1/dashboard/yield` supplies per-day developer runs, verification returns, merges and yield ratios to the
  Dashboard `yield` block.

To add another mechanical hand-over guard, add its decision and focused unit tests in `aiflow/v4/worker/mechanical.py`,
call it before the existing gate and push in the developer publish path, and document its behavior here. Keep SQL in
`aiflow/v4/control/repositories/developer_runs.py`; keep the Dashboard panel and its data contract together in the
yield route, analytics, and console extension.

## Dashboard figures

Daily rows use UTC run start dates. Completed developer runs are ended `dev` runs; daily merges are distinct tickets
with an ended `verify` run whose outcome is `merged`, while the overall merge count deduplicates tickets across the
full window; returns are ended verify runs with outcome `rework`. The yield ratios are merged tickets per 100 completed
developer runs, completed developer runs per merged ticket, and all ended run tokens started that day per ticket
merged that day. A zero developer-run denominator makes per-100-run ratios unavailable. A day with no merged ticket has
no runs-per-merge or tokens-per-merge value. The overall target is above 40 merged tickets per 100 completed developer
runs.

## Routes

- `GET /v1/dashboard/yield` returns daily delivery-yield metrics for the projects visible to the caller.
