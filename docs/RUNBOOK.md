# AI Flow runbook (one page)

You work in Jira. The workers on WSL do the rest. Jira project JAIF, repository `jooservices/ai-flow`.

## Create a ticket
1. Create it in Backlog with the template (README, "Ticket template"): Business requirements, AC, Scopes, Tests, Technical (a Task also has Code structure). Set Type (Feature, Bug, Hotfix, Chore, Docs, Refactor) and, only when you want another default, Agent (AI), AI Model, Require human, Priority.
2. Move it to **To Do**. A ticket without AC, Scopes or Tests goes back to Backlog with a comment saying what is missing. Label `aiflow-hold` keeps a ticket out of the flow.
3. A ticket that has child tickets is a container: only its leaves are worked, and it becomes Done when all children are Done.

## What happens
To Do: developer (default Codex gpt-6-luna xhigh; Highest priority: Claude) posts a Plan, codes, comments what changed, moves it to In Review.
In Review: reviewer (default Grok) comments a checklist; PASS: Review passed; FAIL: back to To Do with label `rework`.
Review passed: the supervisor opens a draft PR (the repository's template). In QA: CI green, the PR is marked ready, the bots comment, feedback is fixed by the developer (rework), QA (default Claude) comments a checklist; PASS: merge and Done. Require human = Yes: Human review, you move it to Ready to merge.

## Blocked
The ticket states why in the comments and in the field Blocked reason (`Kind: reason`). Answer in a comment, then move the ticket from Blocked to **To Do**: the developer starts a new session and reads your answer. `Quota` and `Offline` tickets return by themselves when an AI is available again.

| Kind | Meaning | You do |
|---|---|---|
| Unclear | the developer could not understand the ticket | answer in a comment, move to To Do |
| Attempts | an AI failed three times | read the comments, fix the cause or the ticket, move to To Do |
| CI | CI did not finish or has no result in 90 minutes | look at the PR checks, move to To Do |
| Config | repository or branch problem | fix it, move to To Do |
| Human | a human review still requests changes | resolve it, move to To Do |
| Quota, Offline | no AI available | nothing; it returns by itself |

## Operate (on WSL, `cd ~/Workspaces/JOOservices/projects/ai-flow`)
- Status: `systemctl --user list-units 'aiflow-*'`; health: `bin/aiflow-health` (alerts are comments on JAIF-12, plus a daily AI usage summary).
- Pause / resume: `bin/aiflow-work pause` (workers finish the current step and take nothing new), `bin/aiflow-work resume`.
- Pick up new code or configuration: `git pull`, `bin/aiflow-work check`, `bin/aiflow-work restart` (workers leave after their step and systemd starts them again).
- Logs: `journalctl --user -u 'aiflow-work@*' -f`, AI logs in `Workspaces/.tmp/aiflow/logs/`.
- Configuration: `Workspaces/JOOservices/config/aiflow.toml` and `config/aiflow/*.toml` (examples in `config.example/`); secrets only in `Workspaces/.env`.
- An AI that lost its login is probed out of the pool (health reports `ai-<machine>-<AI>`): sign it in again (`grok login --device-code`, `codex login`, `claude`), the next probe puts it back within three hours.
- Tool versions of the machines are compared daily; a drift is reported by the health check.
