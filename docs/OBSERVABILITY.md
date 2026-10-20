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
| `ticket.pickup` | `status`, `role`, `agent`, `rework` |
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
| `memory.recall` / `memory.propose` / `memory.case` / `memory.flush` / `memory.curate` | `found`, `ids`, `kind`, `title`, `written`, `rework_rounds`, `sent` |
| `setup.step` | `text`, `applied` |

## Questions to ask (SQL in OpenObserve)
- Time a ticket spent in each status: `SELECT _timestamp, "to" FROM "aiflow_events" WHERE event = 'jira.transition' AND ticket = 'JAVM-82' ORDER BY _timestamp`
- Minutes of AI per agent and role: `SELECT agent, role, sum(minutes) FROM "aiflow_events" WHERE event = 'ai.run' GROUP BY agent, role`
- Rework rounds per project: `SELECT project, count(*) FROM "aiflow_events" WHERE event = 'ticket.rework' GROUP BY project`
- What happened to a ticket: `SELECT _timestamp, event, message FROM "aiflow_events" WHERE ticket = 'JAVM-82' ORDER BY _timestamp`
