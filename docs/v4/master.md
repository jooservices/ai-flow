# Master monitoring cycles

Master accounts provide one control-wide monitoring cycle at a time. The worker asks the control API for a cycle when its account has the `master` role, its account status is `idle`, `working`, or `probing`, and its node is online. The cycle uses the Redis lease `master:global` and the normal run heartbeat and fencing checks. The lease is not tied to Claude or Codex, so any ready master account may claim the next cycle. A cooldown, signed-out/error account, offline node, or expired lease excludes that account, and the next cycle goes to another ready master account. For one cycle (`MASTER_CYCLE_S`, one minute) after a quota or lost-lease failure, accounts of the failed provider wait while the other provider has a ready master account, so Claude hands over to Codex and back. If the other provider has no ready master account, or that minute has passed, any ready master account (including one of the same provider) takes the cycle, so the flow never stops. Successful cycles are spaced at least one minute apart; normal failed cycles use the usual backoff, while a quota or lost-lease failover claims immediately.

## Monitoring snapshot

`GET /v1/master/context` returns the snapshot for the active fenced run:

- Open incidents, including node, role coverage, run clock, quota, and ticket incidents.
- Tickets in Needs owner that do not have the `owner-escalated` label.
- Running ticket runs with elapsed minutes, project-specific `work_minutes` target/warn/hard limits, and which clocks have passed.
- Offline nodes.
- Accounts in cooldown, signed out, error, or with an open `account_quota` or `quota_high` incident, including quota windows and reset data.

The worker runs `prompts/v4/master.md`, returns a JSON action plan, and submits each action to
`POST /v1/master/actions`. This endpoint checks the active run and fence, applies the owner escalation policy, then
performs allowed Jira, incident and notification actions and records the result in the audit log. An `incident_open`
action must use a kind listed in the incident runbooks. Tool calls are denied for master runs so all writes stay
behind the control API.

## Allowed actions and audit

The control API allows `jira_comment`, `jira_update`, `dev_reply`, `requeue` (only to To Do), `incident_open`, `incident_close`, `notify_owner`, and `escalate_to_owner`. Every action checks the active run and fence, records the account and run in `write_audit`, and emits a `master.action` event. Other actions are refused with a reason. Master Jira comments use the shared `AI Flow · master · …` note shape with What, Why, Result, Next, and a Run line containing account, provider/model, node, run, and fence.

The mandatory `escalate_to_owner` policy covers infrastructure changes, secret or credential changes, and production releases. These requests are never executed by the master. The control API comments on the related Jira ticket when available, notifies the owner, records the refused request and escalation, and returns the policy reason. The policy matches what is requested: the action name, its `intent`, and an optional explicit `category` (`infrastructure`, `secret`, `production_release`, always escalated). Observation fields (`what`, `why`, `result`, `detail`, `title`) are not matched, so an `incident_open` for an offline node or a `requeue` after the hard limit stays an allowed follow-up. The default patterns need a change verb next to a protected object (for example "change … infrastructure", "rotate … API key", "release … production"); plain words such as node, server, database, or restart do not escalate. The code keeps these default rules active if `policy/default.yaml` does not define them; the control repo can add further regular-expression patterns under `escalate_to_owner`.

Master monitoring does not change infrastructure, secrets, or release production. The owner decides whether and how those requests may proceed.
