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

## Developer pool (`developer = "Any"`)
With `developer = "Any"` in `[defaults]` or a project file, the developer step goes to the AI with the lowest `running sessions / ai_limit` on that machine (ties follow `agent_order`; an AI with a limit of 2 or more keeps its last slot for review and QA). A ticket whose Agent (AI) field names an AI, Priority Highest and the AI that already worked on a ticket (rework) behave as before. The model of the chosen AI is the entry of `[runtime] agent_models` (`Claude:sonnet`); without an entry the AI uses its own default. Deploy this code to every worker before any file says `Any`: an older worker would treat `Any` as an unknown AI. Switch back with `developer = "Codex:gpt-6-luna:xhigh"` and `bin/aiflow-work restart`.

## Add a machine to the pool

Each machine runs its own workers and configuration while sharing the Jira queue and Redis state. Before starting a new machine, make sure it can reach the configured shared Redis, OpenObserve, and memory endpoints, directly or through the appropriate SSH tunnel. For Redis, use `bin/aiflow-redis-tunnel` when the machine cannot route to Redis; `deploy/launchd/com.jooservices.aiflow-redis-tunnel.plist` is its macOS LaunchAgent template. Before loading that template, replace the account-specific absolute paths in `ProgramArguments` (the `bin/aiflow-redis-tunnel` script) and `StandardErrorPath` (the log file) with paths valid for the target macOS account; the log directory must already exist. Redis connection settings are `AIFLOW_REDIS_*` in the machine's `.env`; OpenObserve and memory endpoints are `obs_url` and `memory_url` in `[runtime]` of `aiflow.toml`. Keep credentials in `Workspaces/.env`.

1. **Prepare the machine.** Clone the AI Flow repository and the repositories for the projects this machine will serve. Sign in to the AI CLI tools it will run and sign `gh` in to the account that owns the repositories.
2. **Set the machine identity.** In the AI Flow `.env`, set `AIFLOW_MACHINE` to a unique machine name, such as `mac`. It must match the name of the machine table in `aiflow.toml`.
3. **Configure workers and project files.** In `aiflow.toml`, add `[machines.<name>]` with `workers` set to the number of shared workers for this machine. Set optional `[machines.<name>.ai_limits]` entries for the AI tools to cap concurrent sessions. Set `AIFLOW_CONFIG_DIR` to the absolute path of this machine's config directory, which contains `aiflow.toml` and an `aiflow/` directory. Include only project TOML files for repositories cloned on this machine. The worker loads every project TOML in that directory, so omitting projects that are not cloned here prevents this machine from taking their tickets. Export the same value in the shell for setup and validation commands (for example, `export AIFLOW_CONFIG_DIR=/path/to/machine/config`). Add `Environment=AIFLOW_CONFIG_DIR=/path/to/machine/config` to both systemd services (`aiflow-control.service` and `aiflow-work@.service`); on macOS, set it in the `EnvironmentVariables` dictionary of each worker LaunchAgent.
4. **Start the workers.** On Linux/WSL, with `AIFLOW_CONFIG_DIR` set in the shell and both systemd services, run `bin/install-wsl-service --control` to install and start the control service, which keeps workers aligned with the configuration. On macOS, configure a LaunchAgent for each worker to run `bin/aiflow-work --id <worker_id>` from the AI Flow checkout, using the IDs from `bin/aiflow-work list` with the same `AIFLOW_CONFIG_DIR`. IDs may be shared numbers such as `2` or project-specific labels such as `JAVM-2`. The repository does not include a worker LaunchAgent plist; `deploy/launchd/com.jooservices.aiflow-redis-tunnel.plist` is an example LaunchAgent for the Redis tunnel.
5. **Check the machine.** Run `bin/aiflow-work list` and `bin/aiflow-work check` with the same `AIFLOW_CONFIG_DIR` environment used by the workers; confirm the machine name, worker count, AI limits, project files, and configured OpenObserve and memory settings. Then confirm that shared Redis has a heartbeat key `hb:<machine>:<worker_id>` for each running worker, where `<machine>` is `AIFLOW_MACHINE` and `<worker_id>` is the full worker ID, such as `2` or `JAVM-2`.

**Example: the vm machine.** The VM `ai-workspace-1` joined the pool as `AIFLOW_MACHINE=vm` on 2026-10-20.
Its systemd user unit `aiflow-wsl-tunnel.service` runs `ssh -N` to `joos@192.168.2.10`, forwarding local Redis `16383`, OpenObserve `15081` and memory `8751` to the WSL ports `16382`, `15080` and `8750` (the same local ports as the Mac).
`aiflow.toml` has `[machines.vm]` with `workers = 4` and `ai_limits` codex 4, claude 1, grok 1, opencode 0.
On WSL the tunnel key in `authorized_keys` is restricted with `permitopen` to those three ports.

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
- **Read the audit comment**: it starts `AI Flow operator (<AI>) on <machine> handled <incident>`. `Saw` is the incident summary, `Outcome` says RESOLVED, ESCALATE or NO-ACTION, `Did` lists completed actions and `Refused` lists requested actions the supervisor rejected. Limit escalations use `system` as the actor and include the exhausted limit in the outcome. Ticket incidents are commented on their ticket; worker and AI-pool incidents use the health ticket. The AI logs are `Workspaces/.tmp/aiflow/logs/operator-*.log`.
- **Questions for you**: tickets with the label `needs-owner` keep their current status (a Blocked ticket stays Blocked); the comment starts with the question, the options and the recommendation. Answer in a comment and move the ticket to To Do.
- **Switch it off**: `bin/aiflow-work operator off` sets the separate operator switch in Redis; restore it with `bin/aiflow-work operator on`. `bin/aiflow-work check` shows that runtime switch and whether the whole flow is paused. `bin/aiflow-work pause` also stops the operator and workers from taking new work; resume with `bin/aiflow-work resume`. Setting `enabled = false` in the policy file disables the operator by policy.
- **Limits**: when an incident reaches its per-ticket daily limit or the global hourly/daily cap is exhausted, the operator does not start another AI run. It marks the ticket `needs-owner`, posts the question and a system audit comment, then waits for owner review.
- **Drill**: on a real test ticket, verify that an answerable Blocked question is answered and moved to To Do, and that a credential question gets `needs-owner` with no automated resolution. Keep the operator audit comments on both tickets as the evidence.

## The control and the workers of each project
`aiflow-control.service` (one per machine) keeps the running workers equal to the TOML files and runs the operator. Install: `bin/install-wsl-service --control`.
- **Workers of a project**: `workers = N` in `config/aiflow/<KEY>.toml`; worker `<KEY>-1` serves only that project, `<KEY>-2` ... take their project first and then other projects (turn by turn). `[machines.<name>] workers` are shared workers; `max_workers` is the machine's cap (a total above it is refused).
- **Add or change**: edit the TOML; within 30 seconds the control starts the missing workers, or asks the surplus ones to leave after their current step (they exit with code 3 and the control disables the unit). Nothing running is killed.
- **A changed file** (any of `aiflow.toml`, `aiflow/*.toml`, `operator.toml`) makes every worker restart after its step and the control restart itself.
- **Look**: `bin/aiflow-control --dry-run` prints what it would start, retire and disable; `bin/aiflow-work check` shows the workers, the caps and warnings; `bin/aiflow-work list` prints the ids.
- **A wrong file** changes nothing: the control keeps the current workers and the health check reports it on the health ticket. It never stops every worker.
- **If the control stops**, the workers keep working; systemd restarts it after 15 seconds and the health check alerts when its heartbeat is missing.

## Events in OpenObserve
Every process sends its events to OpenObserve (`docs/OBSERVABILITY.md`: events, fields, SQL to ask "what happened to ticket X"). It needs `obs_url` in aiflow.toml `[runtime]` and `AIFLOW_OBS_USER` / `AIFLOW_OBS_PASSWORD` in `Workspaces/.env`; without them nothing is sent. A down OpenObserve never stops a worker.
