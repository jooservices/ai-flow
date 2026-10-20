# AI Flow runbook (one page)

You work in Jira. The workers on WSL do the rest. Jira project JAIF, repository `jooservices/ai-flow`.

## Create a ticket
1. Create it in Backlog with the template (README, "Ticket template"): Business requirements, AC, Scopes, Tests, Technical (a Task also has Code structure). Set Type (Feature, Bug, Hotfix, Chore, Docs, Refactor) and, only when you want another default, Agent (AI), AI Model, Require human, Priority.
2. Move it to **To Do**. A ticket without AC, Scopes or Tests goes back to Backlog with a comment saying what is missing. Label `aiflow-hold` keeps a ticket out of the flow.
3. A ticket that has child tickets is a container: only its leaves are worked, and it becomes Done when all children are Done.

## What happens
To Do: developer (default Codex gpt-6-luna xhigh; Highest priority: Claude) posts a Plan, codes, comments what changed, moves it to In Review.
In Review: reviewer (default Grok) comments a checklist; PASS: Review passed; FAIL: back to To Do with label `rework`.
Review passed: the supervisor opens a draft PR (the repository's template). In QA: CI green, the PR is marked ready, the bots comment, feedback is fixed by the developer (rework), QA (default Claude) comments a checklist; PASS: merge and Done. Require human = Yes: Human review, you move it to Ready to merge. A Deployment section in the developer hand-over also requests Human review by default; the project setting `human_on_deployment = false` skips that automatic stop and records the decision in a ticket comment. Require human = Yes and Release tickets still need approval.

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
- Configuration: `Workspaces/JOOservices/config/aiflow.toml` and `config/aiflow/*.toml` (examples in `config.example/`); secrets only in `Workspaces/.env`. Set optional `[machines.<name>.ai_limits]` entries for `codex`, `claude`, `opencode` or `grok` to cap sessions shared by that machine's workers. Missing entries are unlimited; zero disables an AI. Check the parsed values with `bin/aiflow-work check`.
- If a machine's AI limit is full, workers leave the ticket eligible and wait or take another eligible step; the health ticket's daily usage summary reports peak concurrent sessions per AI and machine. A dead worker's slot expires after its session lease.
- An AI that lost its login is probed out of the pool (health reports `ai-<machine>-<AI>`): sign it in again (`grok login --device-code`, `codex login`, `claude`), the next probe puts it back within three hours.
- Tool versions of the machines are compared daily; a drift is reported by the health check.
- A configured post-merge command runs in the project's main clone after a base-branch merge. Its output is in `.tmp/aiflow/logs/`; failures and timeouts are commented on the ticket and reported by health until a later run succeeds.

## Onboard a project (one Jira project, one repository)
Do this once per project, on the machines that run workers. The first week is deliberately careful.
1. **Repository**: it must be on GitHub under `jooservices`, cloned at `Workspaces/<company>/projects/<repo>` on WSL, with a `develop` branch (and `master` for hotfixes and releases), CI that runs on pull requests, Conventional Commits enforced (commitlint and a PR-title check), and branch protection on `develop` and `master`: pull request required, the CI checks required, up to date, conversations resolved, no approvals needed (all AIs share one GitHub account).
2. **Jira**: run `bin/aiflow-setup <KEY>` (dry run: prints what is missing), then `bin/aiflow-setup <KEY> --apply`. It creates only what is missing and never changes what exists: the fields Agent (AI), Workspace, Blocked reason, Require human, AI Model and Type, Version (found by name, so ids may differ per Jira site), their options, the issue type Release, the workflow "AI Flow Workflow v2" and its scheme, then assigns the scheme to the project. If Jira refuses the assignment because the project has issues in statuses the new workflow lacks, switch with `POST /rest/api/3/workflowscheme/project/switch` and a status mapping. Still manual: a Kanban board with one column per status (JAIF Kanban is the model).
3. **Configuration**: add `JOOservices/config/aiflow/<name>.toml` on every machine (the directory is git-ignored) with `[project] jira_key`, `repo`, `company`, and for the first week `auto_merge = false`, `ci_required = true`, `require_human = true`; set `release_check` to the command that runs the full test suite (and e2e). `human_on_deployment` defaults to `true`; set it to `false` for a project that skips the automatic Human review caused by a Deployment section. This setting does not override Require human = Yes on a ticket or Release approval. Optionally set `database = "<shared-project-name>"` to provision an isolated shared database for each ticket; the target repository must ignore `.env`. The supervisor creates a worktree `.env` and verifies it before developer, reviewer, and QA sessions; it reuses the database during rework and tears it down when the ticket completes or its worktree is removed. Teardown failures appear in the health check. An optional `deploy_command` runs after merges into `base` from the project's main clone. The supervisor prepares a clean checkout at the exact remote base commit under the per-project lock; it skips the command and reports health if it cannot prepare that state. While the command runs, the worker heartbeat and lock are renewed, and timeout terminates the full process group. Set `deploy_timeout_minutes` (default 30) and leave `deploy_on = "base"` (default) to skip hotfix/release merges, or use `deploy_on = "all"` to include them. Run `bin/aiflow-work check`, then `bin/aiflow-work restart`.
4. **Try**: create one small ticket with the template, label `aiflow-hold`, move it to To Do after removing the label, and follow it until Done. With `auto_merge = false` the ticket waits after QA for the owner to merge.
5. **Open up**: after a week of clean tickets set `auto_merge = true` (keep `require_human = true` for risky tickets, set per ticket with the field Require human).

## Jira hierarchy
Jira allows Epic -> Story, Task or Bug -> Sub-task. A Story cannot contain Tasks; to split a Story use Sub-tasks, to group Stories and Tasks use an Epic. Only leaves are worked: a ticket with children is a container that closes itself when all its children are Done (drill D8).

## Operator (incidents handled by the supervisor)
Policy and limits: `docs/OPERATOR.md`; thresholds: `JOOservices/config/operator.toml` (off without the file or with `enabled = false`).
- **Read what it did**: every run is an audit comment on the ticket (on the health ticket for a worker or AI-pool incident): `AI Flow operator (<AI>) ... Saw / Outcome / Did / Refused`. The AI logs are `Workspaces/.tmp/aiflow/logs/operator-*.log`.
- **Questions for you**: tickets with the label `needs-owner` stay Blocked; the comment starts with the question, the options and the recommendation. Answer in a comment and move the ticket to To Do.
- **Switch it off**: `bin/aiflow-work operator off` (on: `operator on`), or `bin/aiflow-work pause` for the whole flow, or `enabled = false` in the policy file followed by `restart`.
