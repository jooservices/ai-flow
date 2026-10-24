# Observability: every event goes to OpenObserve

Each process (worker, control, operator inside the control, health check, `aiflow-setup`) sends JSON events to one OpenObserve stream (`aiflow_events`),
next to the journal line it always had. Sending is asynchronous: events are queued in memory and sent in batches; when OpenObserve is down the
newest events are kept (up to 5000) and the work never waits or fails. Without `AIFLOW_OBS_USER` / `AIFLOW_OBS_PASSWORD` (Workspaces/.env) and
`obs_url` ([runtime] of aiflow.toml) nothing is sent. Which OpenObserve organization and account to use is decided at provisioning time.

Every row has `_timestamp` (microseconds), `event`, `service=aiflow`, `machine`, `worker` (`JAVM-2`, `1`, `control`, `health`, `setup`), `pid`, and when it
concerns a ticket `ticket` and `project`.

| Event | Fields (besides the common ones) |
|---|---|
| `log` | `message`: every journal line of the supervisor |
| `worker.start` / `worker.stop` / `worker.retired` / `worker.restart` / `worker.error` | `home`, `projects`, `error` |
| `ticket.pickup` | `status`, `role`, `agent`, `rework`, `waited_minutes` (time in the status before a worker took it) |
| `ticket.status` | `from_status`, `to_status`, `minutes_in_from`, `source` (`supervisor`: it made the move; `observed`: someone else did, noticed at the next poll, at most one poll late) |
| `step.run` | `status`, `role`, `agent`, `seconds`: the work one worker did on a ticket in one pass |
| `ai.usage` | `agent`, `role`, `model`, `input`, `output`, `cache_read`, `cache_write`, `reasoning`, `total`, `cost_usd_list`, `turns`, `usage_known`, `minutes`: tokens of one AI run, read from its log |
| `ai.assign` / `ai.limit` / `ai.unserved` | `role`, `agent`, `limit`, `pool`, `cooling` |
| `ai.run` | `role`, `agent`, `model`, `effort`, `minutes`, `why`, `code`, `verdict`, `quota`, `log_file` |
| `ai.quota` / `ai.failed` / `ai.probe` | `agent`, `cooldown_minutes`, `attempt`, `state` |
| `jira.transition` | `to`, `ok`, `error` |
| `jira.comment` / `jira.create` / `jira.link` / `jira.agent` / `jira.field` / `jira.label_added` / `jira.label_removed` | the ticket and what changed |
| `ticket.blocked` / `ticket.unblocked` / `ticket.resumed` / `ticket.rework` / `ticket.human_review` / `ticket.done` | `kind`, `reason`, `round`, `pr` |
| `gate.decision` | `action`, `detail`, `ci`, `behind`, `conflicting`, `feedback`, `stuck`, `qa_ok`, `draft`, `waited_minutes` |
| `github.pr_open` / `pr_ready` / `update_branch` / `merge` / `pr_comment` / `ci_rerun` | `repo`, `number`, `head` |
| `deploy.queue` / `deploy.result` | `project`, `eligible`, `ok`, `exit_code`, `timed_out`, `log` |
| `control.start` / `control.cycle` / `control.config_changed` / `control.error` / `control.worker_started` / `worker_retiring` / `worker_disabled` / `worker_kept` | `start`, `retire`, `disable`, `worker`, `message` |
| `operator.incident` / `operator.resolved` / `operator.run` / `operator.action` / `operator.escalate` / `operator.notify` / `operator.limit` | `incident`, `kind`, `agent`, `result`, `action`, `question` |
| `health.check` / `health.problem` / `health.alert` | `problems`, `kinds`, `kind`, `message` |
| `memory.recall` / `memory.propose` / `memory.case` / `memory.flush` / `memory.curate` | `found`, `ids`, `case_ids` (recall only: recalled study-case IDs), `kind`, `title`, `written`, `rework_rounds`, `sent` |
| `setup.step` | `text`, `applied` |

## Questions to ask (SQL in OpenObserve)
- Time a ticket spent in each status: `SELECT _timestamp, "to" FROM "aiflow_events" WHERE event = 'jira.transition' AND ticket = 'JAVM-82' ORDER BY _timestamp`
- Minutes of AI per agent and role: `SELECT agent, role, sum(minutes) FROM "aiflow_events" WHERE event = 'ai.run' GROUP BY agent, role`
- Rework rounds per project: `SELECT project, count(*) FROM "aiflow_events" WHERE event = 'ticket.rework' GROUP BY project`
- What happened to a ticket: `SELECT _timestamp, event, message FROM "aiflow_events" WHERE ticket = 'JAVM-82' ORDER BY _timestamp`

## Time per step and tokens
- **Time in each status** comes from the processes: a move made by a worker or the control is recorded when it is made (with the status it left and the minutes spent there); a move made by the owner or by Jira is noticed at the next poll of any worker (one event, whichever worker sees it first). The Jira `updated` time is not used (a comment changes it too). Time of the first sight of a ticket has no `from_status`.
- **Tokens** are read from the log the AI run already wrote: Claude (`result` event: input, output, cache read/write, thinking, list-price cost), Codex (`--json` turn events; an old plain log gives the total only), OpenCode (`--format json` step events). Grok's output was not verified (no balance), so its runs say `usage_known=false`. `cost_usd_list` is the cost at list price, not money paid.
- Events that cannot be sent when a process stops are written to `Workspaces/.tmp/aiflow/events-<machine>-<worker>.jsonl` and sent by the next process of the same worker.
- SQL: average minutes per status `SELECT from_status, round(avg(minutes_in_from), 1) FROM "aiflow_events" WHERE event = 'ticket.status' GROUP BY from_status`; tokens per AI and role `SELECT agent, role, sum(total), sum(output), round(sum(cost_usd_list), 2) FROM "aiflow_events" WHERE event = 'ai.usage' AND usage_known = true GROUP BY agent, role`; queue wait `SELECT project, round(avg(waited_minutes), 1) FROM "aiflow_events" WHERE event = 'ticket.pickup' AND status = 'To Do' GROUP BY project`.

## Production health dashboard

The provisioned **AI Flow production health and SLO** dashboard uses the existing `aiflow_events` stream. Its default time range is seven days, and every panel query also applies that window so saved or copied queries keep the same bound. `_timestamp` is OpenObserve's timestamp column. Each SQL block below is the query used by the panel with the same name.

“Blocked signature” is the exact `kind` plus `reason` emitted by `ticket.blocked`; the event stream does not normalize free-text reasons. Status time is the time spent in each status for transitions observed in the window. The Done panel treats a human-review event or an externally observed Jira status change as human involvement. Master rescue runs are `operator.run` events, and their `result` is `RESOLVED`, `ESCALATE`, `NO-ACTION`, or `NONE`. Rework signatures are the exact `headline` text. The repeated-signature panel includes only rework after a same-ticket recall that contains at least one study case (`case_ids`).

<!-- dashboard-panel: Blocked by signature -->
```sql
SELECT kind, reason AS signature, count(*) AS blocked
FROM "aiflow_events"
WHERE event = 'ticket.blocked'
  AND _timestamp >= now() - INTERVAL '7 days'
GROUP BY kind, reason
ORDER BY blocked DESC
```

<!-- dashboard-panel: Time in each status -->
```sql
SELECT from_status, round(sum(minutes_in_from), 1) AS total_minutes,
       round(avg(minutes_in_from), 1) AS average_minutes, count(*) AS transitions
FROM "aiflow_events"
WHERE event = 'ticket.status'
  AND minutes_in_from IS NOT NULL
  AND _timestamp >= now() - INTERVAL '7 days'
GROUP BY from_status
ORDER BY total_minutes DESC
```

<!-- dashboard-panel: Done without human action -->
```sql
WITH done AS (
  SELECT ticket, max(_timestamp) AS done_at
  FROM "aiflow_events"
  WHERE event = 'ticket.done'
    AND _timestamp >= now() - INTERVAL '7 days'
  GROUP BY ticket
), human_action AS (
  SELECT ticket
  FROM "aiflow_events"
  WHERE event = 'ticket.human_review'
  UNION
  SELECT ticket
  FROM "aiflow_events"
  WHERE event = 'ticket.status' AND source = 'observed'
)
SELECT 'last 7 days' AS window, count(*) AS done_tickets,
       sum(CASE WHEN human_action.ticket IS NULL THEN 1 ELSE 0 END) AS done_without_human_action,
       round(100.0 * sum(CASE WHEN human_action.ticket IS NULL THEN 1 ELSE 0 END)
             / nullif(count(*), 0), 1) AS done_without_human_action_pct
FROM done
LEFT JOIN human_action ON human_action.ticket = done.ticket
```

<!-- dashboard-panel: Rework per ticket -->
```sql
SELECT ticket, count(*) AS rework_events, max(round) AS highest_round
FROM "aiflow_events"
WHERE event = 'ticket.rework'
  AND _timestamp >= now() - INTERVAL '7 days'
GROUP BY ticket
ORDER BY rework_events DESC, ticket
```

<!-- dashboard-panel: Master rescues and result -->
```sql
SELECT incident, result, count(*) AS rescue_runs, round(sum(minutes), 1) AS ai_minutes
FROM "aiflow_events"
WHERE event = 'operator.run'
  AND _timestamp >= now() - INTERVAL '7 days'
GROUP BY incident, result
ORDER BY rescue_runs DESC, incident
```

<!-- dashboard-panel: Repeated signatures after a study case -->
```sql
WITH case_recalls AS (
  SELECT ticket, min(_timestamp) AS recalled_at
  FROM "aiflow_events"
  WHERE event = 'memory.recall'
    AND case_ids IS NOT NULL AND case_ids <> ''
    AND _timestamp >= now() - INTERVAL '7 days'
  GROUP BY ticket
), rework_after_case AS (
  SELECT rework.ticket, rework.headline AS signature
  FROM "aiflow_events" AS rework
  JOIN case_recalls ON case_recalls.ticket = rework.ticket
                   AND rework._timestamp >= case_recalls.recalled_at
  WHERE rework.event = 'ticket.rework'
    AND rework.headline IS NOT NULL AND rework.headline <> ''
    AND rework._timestamp >= now() - INTERVAL '7 days'
)
SELECT signature, count(*) AS repeats, count(DISTINCT ticket) AS affected_tickets
FROM rework_after_case
GROUP BY signature
HAVING count(*) > 1
ORDER BY repeats DESC, signature
```

<!-- dashboard-panel: AI minutes -->
```sql
SELECT agent, role, round(sum(minutes), 1) AS ai_minutes
FROM "aiflow_events"
WHERE event = 'ai.run'
  AND _timestamp >= now() - INTERVAL '7 days'
GROUP BY agent, role
ORDER BY ai_minutes DESC
```

<!-- dashboard-panel: Tokens -->
```sql
SELECT agent, role, sum(input) AS input_tokens, sum(output) AS output_tokens,
       sum(total) AS total_tokens
FROM "aiflow_events"
WHERE event = 'ai.usage'
  AND usage_known = true
  AND _timestamp >= now() - INTERVAL '7 days'
GROUP BY agent, role
ORDER BY total_tokens DESC
```
