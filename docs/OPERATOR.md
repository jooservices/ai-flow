# The operator: what the flow does when it cannot go on by itself

The owner is not in the loop. When a ticket is Blocked, a pull request is stuck, a worker is dead or no AI is usable, the supervisor
(not a person, not a standing AI session) handles it. This page is the policy; the thresholds are in `JOOservices/config/operator.toml`
(example: `config.example/operator.toml`; without the file the operator is OFF).

## How it works
1. **Find** (plain code, no AI, no tokens): the control (`aiflow-control.service`, not a worker) lists the incidents every pass. An incident has a stable id (`kind:subject`), so the same problem is queued once; it leaves the queue by itself when it is gone.
2. **Queue**: Redis list `aiworkflow:operator:queue`, rewritten each pass with one JSON entry per incident.
3. **Run**: one incident at a time, an AI (default Claude, another usable one replaces it) is started **read-only**, without a GitHub login, with a time limit. It reads the facts and asks for actions.
4. **Act**: the supervisor performs only the actions below. Anything else is refused and shown in the audit comment.
5. **Audit**: every run comments what it saw, what it did and why, on the ticket (or on the health ticket for a machine incident).

## Incidents
| Kind | When |
|---|---|
| `blocked` | a ticket is Blocked longer than `blocked_minutes` (Quota and Offline return by themselves: not an incident) |
| `stuck` | a worked ticket has no lease and no change for `stuck_minutes` |
| `ci` | a pull request waits in QA with no change for `ci_wait_minutes` |
| `rework` | a ticket went through more than `rework_limit` rework rounds |
| `rescue` | a Blocked (Config, Attempts, Rework, Unclear) ticket waited its `[master]` time, or a ticket ran over its work limits: Claude takes it, or the owner does; it replaces `blocked`/`stuck` for that ticket (see [master rescue](v4/master-rescue.md)) |
| `worker` | a worker of this machine sends no heartbeat |
| `ai_pool` | no AI is usable on any machine |

Tickets with the label `aiflow-hold` or `needs-owner` are never incidents. Human review waits for the owner by design.

Master rescue records a handover in the JAIF-102 `Handover` status with `Taken by = Claude`. This legacy supervisor does not
schedule work from `Handover`; Claude pickup, use of the AI Model field, and enforcement of the rescue budget depend on the
JAIF-102 Jira workflow/automation. A failed Handover transition leaves no rescue label or record, so the incident can retry after
its temporary cooldown.

## What the operator may do alone
Answer a Blocked ticket's question when the ticket, its parents or the documents answer it clearly; unblock; send a ticket back to To Do with a reason; retry CI; replace a dead AI; restart a worker; pause and resume the flow. As supervisor actions: `comment`, `transition` (To Do, Backlog or Blocked only), `label` (`needs-owner` or `rework` only), `retry_ci`, `replace_ai`, `restart_worker`, `pause`, `resume`; at most 5 per run.

## What always goes to the owner
Spending money; credentials, tokens or secrets; deleting data, databases, repositories or branches; force-push; changing the product direction or the requirements; a release; merging by hand; anything this policy does not cover. When unsure, escalate. The list is `[escalation] always` in `operator.toml`; the operator may only add to it.

## Escalation
The ticket gets the label `needs-owner` and a comment that starts with the question, then the options and the recommended one. Its status is unchanged (a Blocked ticket stays Blocked). Answer in a comment and move the ticket to **To Do**. If a channel is configured in `Workspaces/.env` (`AIFLOW_NOTIFY_CHANNEL=telegram` with `AIFLOW_NOTIFY_TELEGRAM_TOKEN` and `AIFLOW_NOTIFY_TELEGRAM_CHAT`, or `smtp` with `AIFLOW_NOTIFY_SMTP_HOST`, `_FROM`, `_TO`, optional `_PORT`, `_USER`, `_PASSWORD`), the owner also gets one grouped message per `notify_interval_minutes` with the questions and the ticket links, never a secret. Without a channel nothing is sent.

## Limits and switches
- Per ticket: `retries_per_ticket_per_day` operator runs; when reached, the incident is escalated without running an AI. Overall: `max_runs_per_hour` and `max_runs_per_day`; when either cap is reached, queued incidents are escalated instead of starting another run. Limit escalations are cooled for a day and leave a system audit comment. After an AI run the same incident waits one hour.
- `bin/aiflow-work pause` stops the operator too. `bin/aiflow-work operator off` / `on` is a separate runtime switch; `bin/aiflow-work check` shows the policy, operator switch and whole-flow pause state. The `operator` command also reports the switch.
- The AI runs read-only (Claude and Grok: Read, Glob, Grep; Codex: read-only sandbox; OpenCode cannot be restricted and is never used).
- The operator runs inside the control, so a run (up to `run_minutes`) never takes a worker away from tickets.
