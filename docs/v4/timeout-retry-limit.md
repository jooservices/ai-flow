# Raise the developer hard limit after each timeout

A developer run that hits the project hard clock used to return to To Do and be claimed again with the same
60-minute budget. After one timeout the next developer claim receives a larger hard limit.

`POST /v1/work/claim` for step `dev` sets `project.work_minutes.hard` to
`base + 30 * previous_timeouts` for that ticket. Previous timeouts are ended developer runs whose `outcome` or
`error_kind` is `timeout`. A ticket with zero recorded timeouts keeps the project hard limit. Verify, intake and
other steps are unchanged.

The worker stores an adapter `error_kind=timeout` as run `outcome=timeout` (not `failed`) so the next claim can
count it. Hold, lease, split-parent and backoff rules are unchanged. This page does not describe suite handover or
Dashboard charts.

**Console screens:** none (claim payload; no UI in this ticket).
