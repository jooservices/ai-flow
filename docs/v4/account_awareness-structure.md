# Account awareness structure

`aiflow.v4.core.quota.account_awareness` assembles provider quota reports and run history into the shared account
awareness result.

Its named steps are `_awareness_periods` and `_base_rates` for the common time ranges, `_estimated_window` when no
provider windows are reported, and `_reported_windows` / `_reported_window` for provider data. `_weekly_usage` and
`_largest_run` build the account usage summary. To add a section, give it a focused module-level function with
explicit inputs, then call it directly from `account_awareness` and characterize its output.

Routes: `/v1/dashboard/quota`, `/v1/capacity`, `/v1/accounts/{account_id}/probe`,
`/v1/accounts/{account_id}/quota-refresh/result`, `/v1/runs/{run_id}/result`, `/v1/master/autonomy`.
