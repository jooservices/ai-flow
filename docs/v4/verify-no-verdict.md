# Epics and verification runs without a verdict

**Console screens:** Dashboard (`cc`), Work board (`work`) and Delivery (`delivery`).

Two faults stopped merging on 2026-10-24: epics with the ready label were offered as work, and verification runs
that ended without a `VERDICT:` line were retried on the same weak accounts. Both are handled by rule, without AI.

## Epics are never work

A ticket of type Epic is never offered as development, verification, triage, post-merge or intake work, and never
counts as waiting work for the idle-flow check or the capacity plan, whatever its labels (including `aiflow-ready`).
A claim for an epic answers "not eligible".

The control loop moves an epic with its children (`jira_sync`, `_reconcile_epics`):

- **In Progress** when its first child leaves Backlog, Intake and To Do.
- **Done** when every child is Done or Declined.
- Back to **To Do** when a Done epic gets new, active child work (unchanged).

## A verify run without a verdict

The verify worker marks such a run with `details.no_verdict = true` (outcome `failed`, summary `no verdict ...`).
Quota and sign-in problems are not no-verdict results. `control/verify_retry.py` decides the rest. All counts are per
development round: a newer finished dev run starts them again.

- **Retried at once.** A no-verdict run does not start the usual retry backoff. The ticket is offered immediately to an
  account that has not ended it without a verdict. The account that failed is not asked again while another eligible
  account (verify role on, not paused, not the author, label rules respected, on an online machine) has not tried and can actually be offered this ticket by the normal candidate path (machine
  accepting work, project, capacity and readiness gates). An untried account on an online but ineligible machine does
  not hold the retry back.
  When every eligible account has tried once, they may try again.
- **Role health.** The first no-verdict result of an account on a ticket is not scored. The second one in a row on the
  same ticket counts as a failed verify run. A real verdict in between breaks the streak.
- **Barred.** An account with two consecutive no-verdict results on a ticket is not offered that ticket again in this
  round.

## Incident

When every eligible account is barred from a ticket in verification, the watchdog opens one `verify_no_verdict`
incident (subject: the ticket) that names the ticket and the reason and lists the accounts. Repeated checks do not open a
second one. It stays open if temporary capacity loss leaves no eligible account online. It closes when the ticket leaves
verification or an unbarred eligible account can receive it; a new development round resets the account history and can
make a retry available. Incidents are shown in the control UI like all others.

## Dashboard

The Dashboard and Delivery section "Needs attention" lists every ticket that has waited more than 30 minutes for
verification (since the dev run finished), with the accounts that tried, their no-verdict counts and which are not
offered the ticket again. The list is complete rather than truncated at 20 entries. Owner-token and open-console
callers see account IDs; restricted authenticated users see verifier labels and only projects they can view. The data
is `slow_verifications` in `/v1/state`.

## API contracts touched

- `POST /v1/work/claim` refuses an epic, and a verification this account may not take (barred, or an untried account
  goes first).
- `GET /v1/capacity` leaves epics out of the idle and capacity counts.
- `GET /v1/state` carries `slow_verifications` for the Dashboard.
- `GET /v1/deploy/state` leaves epics out of the work counts.
- `GET /v1/master/candidate` polls the master-cycle candidate for an account and node.
- `GET /v1/version-releases/candidates` lists configured Jira versions eligible for a release worker; `POST /v1/version-releases/claim` validates and claims a version for that worker.
- `POST /v1/runs/{run_id}/result` records the fenced worker result; a verification with no verdict is marked in the stored run details.

An untried account defers the retry only when the final candidate list (`GET /v1/work/candidates`: entitlement,
rollout and the 20-ticket limit included) really offers it this ticket. An account that is not barred but has no
machine that is online and accepting work is not counted as eligible, so it cannot keep a ticket from the incident.
