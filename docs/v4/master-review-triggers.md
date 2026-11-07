# Master review triggers

**Console screens:** `cc`

Each master cycle runs every few minutes. Before it calls the master AI, the worker reads the fenced snapshot from
`GET /v1/master/context` and checks four rules in order. The model is called only when at least one rule finds an item
to decide. A cycle with no such item makes no model call, uses no tokens, does not count against the daily model-time
budget, and records the status line **No action needed this cycle.**

## The rules

| Rule name | Starts a model review when | Source in the snapshot |
| --- | --- | --- |
| `open-incident` | an incident is open, including a `ticket_stuck` incident for a ticket that keeps failing | `open_incidents` |
| `hard-limit` | a running ticket has passed its project's hard work limit | `running_clocks[].passed` contains `hard` |
| `repeated-failure` | the scan finds a repeated failure signature and reports it (see [Repeated flow failures](flow-bugs.md)) | `flow_bug_reports` |
| `dashboard-request` | the owner pressed **Review now** in the Dashboard | `review_requested` |

Owner-queue tickets, offline nodes and accounts out of quota no longer start a review on their own. Quota and offline
symptoms are opened as incidents, so they still start one through `open-incident`.

The rules are pure functions of the snapshot: `review_triggers` in `aiflow/v4/worker/master.py` returns the names of the
rules that matched, and `run_master_cycle` calls the model only when that list is not empty.

## Dashboard request

**Review now** in the Command Center master autonomy panel calls `POST /v1/master/review-requests` (console writers only).
The request sets `master_settings.review_requested_at`. The next snapshot read clears it and reports
`review_requested: true`, so one request starts one review. A request that arrives while a snapshot is being built waits
for the cycle after it.

## Cycle metrics

The same panel shows the last seven UTC days from `GET /v1/master/autonomy` (field `cycles`):

- **Cycles**: master runs started that day.
- **AI calls**: cycles that were not idle (their run details do not have `no_watch`).
- **Cycles with an action**: cycles in which at least one action was accepted (`completed` above zero).
- **AI calls with no action**: the share of AI calls that completed no action. The target is below 5 percent.

## Adding a rule

1. Add a branch to `review_triggers` in `aiflow/v4/worker/master.py` that returns a new name, and keep the rule a plain
   check of the snapshot.
2. If the control plane must supply new data, add it to `master_snapshot` in `aiflow/v4/control/app_helpers.py`.
3. Add a case to `TRIGGERS` in `tests/v4/test_master_review_triggers.py`. A case with no trigger must stay quiet.
