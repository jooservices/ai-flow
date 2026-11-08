# Repeated failure root causes

**Console screens:** `cc`, `delivery`, `knowledge`, `machines`, `review`, `users`

The Dashboard panel is a block in the shared layout system, which also serves Delivery, Human review, Machines and AI,
Knowledge, and Users and roles.

The fenced master scan groups failed and timed-out runs from every project and step. A signature combines the outcome,
error kind, and normalized first line of the summary; ticket keys, numbers, and paths are removed. Three
matching runs in six hours create one `Bug` in the configured AI Flow maintenance project (`JAIF` by default),
labeled `flow-bug` and set to Highest priority. Runs on one ticket count.

The bug records the count, affected tickets and steps, provider accounts, first and last time, three sample messages,
tokens, and estimated cost. Later matching runs update the same bug. At ten runs in six hours, its affected tickets
and step are held from work offers until the bug closes. When a completed bug recurs within 24 hours, the same issue
returns to To Do as a regression and its regression count increases. The `flow-bug` label keeps these bugs ahead of
other developer work.

The Dashboard's **Repeated failures** panel calls `GET /v1/dashboard/flow-failures`. It lists the most common
signatures in the last 24 hours, affected tickets, tokens, the linked bug and its state, regression count, and the
share of terminal runs spent on signatures that already have a bug. Project-scoped console users see only their
projects.

## Routes

- `GET /v1/dashboard/flow-failures` Returns the most common repeated failure signatures in the caller's visible projects.
- `GET /v1/dashboard/waiting` Returns Dashboard-visible Human review ticket keys without exposing queue details.
- `GET /v1/dashboard/review-rounds` Returns merged-ticket review rounds and finding counts in the caller's visible projects (JAIF-351; listed because the Dashboard module also serves the new flow-failures route).
- `GET /v1/dashboard/quota` Returns the AI account quota summary shown on the Dashboard (listed because its module also serves the new flow-failures route).

## Flow and extension points

`flow_error_pattern` builds the signature, `flow_bug_candidates` reads the rolling six-hour run window, and
`process_flow_bugs` creates or updates the fenced Jira issue. `flow_breaker_exclusions` removes blocked ticket-step
pairs in the work eligibility pipeline. The Dashboard read model groups the 24-hour run window and joins it to the
signature tracker.

Adding a workflow step requires no detector registration: ensure finished runs write the step, outcome, error kind,
summary, provider, account, token, and cost fields. The step becomes part of the signature automatically. Keep
quota, authentication, offline-node, and missing-tool incidents in their existing incident paths.

## Data design

- The detector and Dashboard read terminal failed and timed-out rows with `coalesce(ended_at,started_at)` inside
  six or 24 hours. The detector uses the partial `runs_failure_signature_recent` index. Work eligibility joins
  active tracker rows to `flow_bug_blocked_tickets` by signature and ticket key.
- `flow_bug_tracker.signature` remains the primary key, so there is one tracker and one Jira key per signature. The
  tracker holds scalar counters, timestamps, token and cost totals, breaker state, and regression watch state.
  `flow_bug_signature_members` stores one row per distinct ticket, step, provider, or account for a signature, with
  first and last seen times and an occurrence count. `flow_bug_sample_messages` keeps at most three messages per
  signature, and `flow_bug_blocked_tickets` stores active ticket holds with an index on `ticket_key`.
- Migration 035 is idempotent and does not scan `runs`, shorten `run_ids`, or delete data. Existing trackers seed
  `occurrence_count` from `cardinality(run_ids)`; token and cost totals start at zero, while times, members, and samples
  accumulate as new runs are recorded. The migration adds no array columns or array indexes and can run with the
  normal control database role.
- Member rows whose `last_seen_at` is older than 90 days are removed during each flow-bug master scan. Their matching
  stale breaker-ticket rows are removed at the same time, while tracker identities, cumulative counters, and the
  three sample slots remain. Growth is one row per distinct member value per signature during that retention window,
  rather than one growing tracker array or one row per run.
