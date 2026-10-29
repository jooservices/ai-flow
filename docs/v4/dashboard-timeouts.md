# Developer run timeouts on the Dashboard

The Command Center Dashboard Work tab did not show developer-run timeouts per day or per ticket, so the owner
could not see whether the timeout share stayed below 10 percent.

`GET /v1/dashboard/timeouts?days=30` returns developer-run counts, timeout counts, `timeout_share_pct`,
`target_share_pct` of 10, daily rows, and per-ticket rows for the caller's project scope. Counts use ended
developer runs (`step=dev`) whose `outcome` or `error_kind` is `timeout`. Missing measurements are zero.

The Dashboard Work tab at `/cc` shows a Developer run timeouts panel. Share tone is good below 10 percent and
warn at or above 10 percent.

**Console screens:** `cc`
