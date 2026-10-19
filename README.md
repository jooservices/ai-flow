# ai-flow

> New to this project (person or AI)? Read [docs/CONTEXT.md](docs/CONTEXT.md) first: purpose, rules, the journey of a ticket and how to work with the owner, without technical detail.

Jira-driven multi-AI delivery flow for Workspaces (Jira project `JAIF`). Supervisors on the Mac and the WSL workstation poll Jira, run a developer AI for each ready ticket, a reviewer AI, then drive the pull request through CI, feedback and QA, merge it and move the ticket to Done. The owner works only in Backlog.

## Flow

```
Backlog -(owner)-> To Do -> In Progress (developer) -> In Review (reviewer) -> In QA (pull-request gate) -> Done
                      ^           |                         |                        |
                      |           +-- Blocked <-------------+------------------------+   (owner: Unblock)
                      +---- Review failed / QA failed / CI failed / feedback / conflict  (label `rework`, reason in a comment)
```

| Jira status | Who | Next |
|---|---|---|
| Backlog | owner / any AI creates and refines | owner moves to **To Do** when the ticket is complete (that means "ready") |
| To Do | supervisor claims it (Redis lease), when no "is blocked by" ticket is open; `rework` tickets first | developer AI starts, status becomes In Progress |
| In Progress | developer AI (`Agent (AI)` field, else `AIFLOW_DEVELOPER_DEFAULT`), always from the latest base; no lease for `AIFLOW_ORPHAN_GRACE_SECONDS` means the AI died, so another AI continues | `RESULT: DONE` with valid commits: In Review. `RESULT: BLOCKED: <reason>`: Blocked |
| In Review | reviewer AI (never the developer; `AIFLOW_REVIEWER_DEFAULT` first) | PASS: the supervisor opens the pull request, status In QA. FAIL: To Do + `rework` |
| In QA | the **gate**, run by the supervisor (no AI) except the QA step | see below |
| Blocked | needs the owner | too many attempts or rounds, a human review that still requests changes, a rejected human request, CI that never started |
| Done | | the pull request is merged |

### The gate (status In QA), checked in this order

1. Pull request merged (also by a human): Done. Closed without merging: Declined.
2. Behind the base: a clean merge is made by the supervisor (`update-branch`), a conflict goes back to the developer. Any head change discards the QA result.
3. CI: failed goes back to the developer with the failing log; pending waits until `AIFLOW_CI_WAIT_MINUTES`, then blocks with a timeout reason if still running; a repo in `AIFLOW_CI_REQUIRED_REPOS` with no CI result after `AIFLOW_CI_WAIT_MINUTES` is Blocked.
4. After the last push, wait `AIFLOW_FEEDBACK_SETTLE_MINUTES` for review bots (CodeRabbit, Copilot), then read the feedback: unresolved review threads, new human comments, "changes requested" reviews. Anything open goes back to the developer.
5. QA AI (never the developer, not the reviewer when possible) on the exact pull request head. FAIL goes back to the developer.
6. One pull request per repository is in QA and merge at a time (`qaslot:<repo>`), so a merge does not make the next QA stale twice.
7. Merge (`gh pr merge --match-head-commit`, method `AIFLOW_MERGE_METHOD`) for repos in `AIFLOW_AUTO_MERGE_REPOS` (`*` = all); other repos wait for the owner to merge. Then Done, and the branch and worktree are removed.

### Choosing the AI and the model per ticket

The Jira field **AI Model** is a list of `<AI>:<Model>`; empty means the default developer, `AIFLOW_DEVELOPER_DEFAULT` (`Codex:gpt-6-luna:xhigh`). The AI part replaces `Agent (AI)` (which the supervisor keeps in sync), the model is passed to the CLI (`-m` for Codex, Grok, OpenCode; `--model` for Claude). The effort is not part of the value: it comes from the role default. Reviewer and QA follow `AIFLOW_REVIEWER_DEFAULT` and `AIFLOW_QA_DEFAULT`. If another AI takes the ticket over (quota), it runs with its own default model.

| AI | Values offered (checked on 2026-10-19) |
|---|---|
| Codex | `gpt-6-luna` (default), `gpt-5.6-terra`, `gpt-5.6-luna` (the models the CLI itself lists) |
| Claude | `fable`, `opus`, `sonnet`, `haiku` (aliases of the latest model; verified) |
| Grok | `grok-4.7`, `grok-4.7-build-fast`, `grok-4.6`, `grok-4.5` (`grok models`) |
| OpenCode | `opencode/deepseek-v4.1-flash-free` (easy tasks), other free `opencode/*` models, `opencode-go/*` and `openai/*` models (`opencode models`) |

OpenCode only works on tickets that name it (`AIFLOW_EXPLICIT_ONLY_AIS`). To add a model, add the option to the Jira field in the form `<AI>:<Model>`.

### Require human

The Jira field **Require human** = Yes makes the gate stop after CI, feedback and QA passed: the supervisor comments the pull request URL and moves the ticket to **Human review** (nothing else happens to the branch while the human looks, except that new feedback sends the ticket back to To Do). The human moves it to **Ready to merge** (approval) or to To Do with a comment (changes). In Ready to merge the supervisor brings the branch up to date if it is outdated (clean merge by `update-branch`), waits for CI, and merges; QA and review are not repeated. If CI fails or a conflict needs the developer, the ticket goes back to To Do and, after the fix, straight back to Ready to merge. A pull request merged by the human himself moves the ticket to Done. Previews for the human are JAIF-28.

### No AI available (quota, offline)

An AI that is out of quota, has an empty balance or is not signed in goes on a cooldown (`AIFLOW_QUOTA_COOLDOWN_MINUTES`, shared by all machines) and the step goes to another AI. Each supervisor publishes the AIs of its machine (`ais:<machine>` in Redis), so a step that this machine cannot serve but another online machine can is simply left to it. When no online machine can serve a step for `AIFLOW_QUOTA_BLOCK_MINUTES`, the ticket is Blocked, the field **Blocked reason** says `Quota: ...` (some AI cooling) or `Offline: ...` (nobody has the AI), and every `AIFLOW_RECHECK_SECONDS` one supervisor re-checks Blocked tickets of those two kinds and moves them back (`Unblock to <status>`) as soon as an AI is available. Other blocks (Unclear, Attempts, Rework, CI, Human, Config) wait for the owner.

### Rework

A ticket sent back gets the label `rework`, goes to To Do (the Jira workflow of JAIF allows `Review failed` and `QA failed` to To Do) and the reason is kept for the next AI. The developer rebases on the latest base and answers **every** feedback item in a JSON file (`fixed` or `rejected`, with a reason). The supervisor posts the answers on the pull request and resolves the threads. A rejected request from a human is Blocked for the owner. After `AIFLOW_MAX_CYCLES` rounds the ticket is Blocked.

### Conventions

Commits and the pull request title follow Conventional Commits (`type(scope): Subject (KEY)`, uppercase first letter, no final period, at most 120 characters), as the jooservices repositories enforce in CI. The supervisor checks the commit subjects before it pushes; if the developer gave no valid `PR_TITLE:` it builds one from the Jira summary.

The ticket may name `Repo: <name>` (under `JOOservices/projects`) and `Base: <branch>`; defaults come from `.env`. A ticket with label `aiflow-hold` is never picked up. The Jira field `Workspace` shows which machine works on the ticket now.

## Hand-over

Every AI comments on the ticket (format in `prompts/common.md`). The supervisor also comments at start, end, failure and quota events. Activity (log output or a new commit) is watched; no activity for `AIFLOW_IDLE_MINUTES` kills the AI and the ticket is reassigned. A usage-limit error puts that AI on a cooldown (`AIFLOW_QUOTA_COOLDOWN_MINUTES`) and the next AI continues from the branch and the comments. The branch is pushed to origin every two minutes during development, so the other machine can continue.

## Run

```
cp .env.example .env              # set AIFLOW_MACHINE (mac|wsl) and AIFLOW_SLOT
bin/aiflow-supervisor --once      # one pass (exit 3 when nothing was ready)
bin/aiflow-supervisor             # loop
python3 -m unittest discover -s tests -t .
```

### Hotfix and release

Type Hotfix starts from and targets the production branch (`hotfix_base`, master). When a pull request that targets the production branch is merged and the integration branch lacks it, the supervisor creates a ticket "Merge master back into develop after <KEY>" (Type Chore, linked to the original): its developer runs `git merge origin/master`, keeps the merge commit and resolves conflicts, and it goes through review, CI, QA and merge like any other ticket.

A **Release** ticket (issue type Release, field Version `x.y.z` higher than the last tag `v*`, otherwise Blocked as Unclear) uses the release prompt: its branch is cut from the integration branch and its pull request targets production. The AI only runs the project's full check (`release_check` in the project TOML, or the documented full test command and e2e) and reads the result: a failure creates one Bug ticket per failure in Backlog, linked as blocking the release, and blocks the release; otherwise it prepares CHANGELOG, README and manifest versions. Release always waits in Human review: after you move it to Ready to merge the supervisor merges it, creates the tag and the GitHub release (`tag_prefix` + version, generated notes) and opens the merge-back ticket.

### Operations

One page for the owner: [docs/RUNBOOK.md](docs/RUNBOOK.md). Every `AIFLOW_PROBE_HOURS` (3) each installed AI is asked for one word; one that is signed out or out of balance leaves the pool (and the health check says so). `bin/aiflow-work pause` / `resume` stop and restart the pickup of new work. Each AI run is logged in Redis (`usage:<day>:...`) and the health check posts yesterday's summary on the health ticket; each machine publishes its tool versions daily and the health check reports a difference between machines.

### What is worked, and in which order

Jira's hierarchy is Epic, then Story or Task or Bug, then Sub-task (a Story cannot contain Tasks). Only leaf tickets are worked: a ticket that has child issues (a Story with Tasks, a Task with Sub-tasks) is a container and the AI never develops it; Epics are never worked. When all children of a container are Done (Declined ones do not count) the supervisor moves the container to Done through the workflow and comments the list of children. The queue is ordered `rework` first, then Priority (Highest to Lowest), then creation time.

### Review, Review passed and the pull request

The reviewer comments a checklist (every AC, scope, tests, security, conventions) and moves the ticket to **Review passed**; a FAIL sends it to To Do with label `rework`. In Review passed the supervisor, with no AI run, opens the pull request: its body is the repository's own `.github/pull_request_template.md` filled from the developer's last comment (What's changed, Why changed, Deployment) plus the ticket link and the AIs involved. A failure to open it is retried (`attempts:<KEY>:pr`) and never counts against the reviewer. The PR is opened as a **draft** and the reviewer's checklist is posted on it; when CI is green the supervisor marks it ready (`gh pr ready`) so the review bots comment, waits `AIFLOW_FEEDBACK_SETTLE_MINUTES` from that moment, and only then judges feedback and calls QA, whose checklist is also posted on the PR. CI may take up to `AIFLOW_CI_WAIT_MINUTES` (90) before the ticket is blocked. Every 30 minutes each machine removes the worktrees of tickets that are Done (with their branches) or Declined (the branch stays). The developer posts a `## Plan` comment before coding; nobody reviews it.

### Ticket template

Every Epic, Story, Task and Sub-task description uses these headings (markdown). A child ticket does not repeat its parent's business: the AI reads the parent chain up to the Epic first (`scripts/jira view`).

```
## Business requirements   what and why of THIS ticket
## AC                      acceptance criteria (QA decides PASS or FAIL with them)
## Scopes                  what is in and out
## Tests                   the test cases needed
## Technical               high level; ### Infrastructure when it matters
## Code structure          Task only: files, classes, layers (the owner defines it while brainstorming)
```

A ticket moved to To Do without AC, Scopes or Tests goes back to Backlog with a comment naming the missing sections; no AI starts. Fields: Type (Feature, Bug, Hotfix, Chore, Docs, Refactor) decides the commit kind and the base branch (Hotfix starts from the project's `hotfix_base`, everything else from `base`); Priority Highest gives the ticket to the project's `highest_developer` (default Claude) unless Agent (AI) or AI Model names an AI; Require human, Agent (AI), AI Model, Workspace and Blocked reason are as before.

The developer's last comment follows a fixed template: What's changed, Why changed, Blocked reason detail, Feature URL, Deployment, Study cases. A Deployment section that is not `None` sets Require human = Yes, so the owner sees the migration or deploy step before the merge.

### Who may give feedback

The repositories are public, so only accounts with write, maintain or admin permission on the repository count as human feedback (comments, change requests, review threads); bots (CodeRabbit, Copilot...) count as before. What anyone else writes never reaches a developer AI: it is left out of the prompt and logged once as `ignored feedback from accounts without write access`. The permission is asked of GitHub once per account and worker.

### Configuration and workers (like `queue:work`)

Secrets stay in `Workspaces/.env`. Everything else is TOML in `JOOservices/config/` (examples: `config.example/`):

- `aiflow.toml`: `[defaults]` for every project and `[machines.<name>] workers = N`, the number of workers of each machine.
- `aiflow/*.toml`: every file is loaded, one `[project]` per Jira project (`jira_key`, `repo`, `company`, `base`, `hotfix_base`, `developer`, `reviewer`, `qa`, `require_human`, `auto_merge`, `ci_required`, `merge_method`, `max_parallel`).

A project value beats `[defaults]`, which beats the old `.env` value (`AIFLOW_*_DEFAULT`, `AIFLOW_AUTO_MERGE_REPOS`...). A machine without these files keeps working with `.env` alone. A ticket can still name `Repo:` and `Base:` in its description as an exception. An empty Require human field means the project's `require_human`.

systemd keeps N `aiflow-work` processes alive per machine; every worker serves all projects and takes the next ticket that is free (`max_parallel` limits one project).

```
bin/aiflow-work --id 2          # worker 2 of this machine (what aiflow-work@2.service runs)
bin/aiflow-work check           # validate the configuration, print what was understood
bin/aiflow-work restart         # graceful restart of every worker (like queue:restart)
bin/install-wsl-service --workers
```

A mistake (unknown setting, the same Jira key in two files, a worker number above the machine's `workers`) stops the worker with exit 2, which systemd does not retry. Heartbeats are `hb:<machine>:<n>` and the health check expects every worker of its machine. The old `aiflow-supervisor@<slot>` keeps working with `.env` only.

State lives in one Redis shared by every machine (plain TCP client in `aiflow/lock.py`: reconnects, retries, and the AI is stopped when Redis stays unreachable for three quarters of the lease time). DB 5 is production, DB 6 is for tests; keys are under `aiworkflow:`. Machines that cannot route to the Redis host use `bin/aiflow-redis-tunnel` (launchd template in `deploy/launchd/`). Logs: `Workspaces/.tmp/aiflow/logs/`. See `.env.example` for every setting.

## Troubleshooting

| Blocked reason | What to do |
|---|---|
| Quota | Wait for another eligible AI to become available; a developer cooling down remains ineligible for review or QA after cooldown, and rechecks unblock only when an eligible AI is available. |
| Offline | Make another eligible AI available; if the only online AI is the developer and it is not cooling down, it is still ineligible for review or QA, so rechecks cannot unblock the ticket yet. |
| Unclear | The owner clarifies the ticket, then unblocks it. |
| Attempts | The owner reviews the failed attempts and decides the next step, then unblocks the ticket. |
| Rework | The owner reviews the work after the maximum rework rounds and decides the next step. |
| CI | The owner resolves CI that never started or timed out, then unblocks the ticket. |
| Human | The owner resolves the outstanding human review request, then unblocks the ticket. |
| Config | The owner corrects the repository or pull request configuration, then unblocks the ticket. |
| Other | The owner checks the recorded reason and decides the next step. |

## Known limits

- A failed cleanup after a merge is reported in the Jira comment, and the worktree must then be removed by hand.
- Codex runs with `danger-full-access` (its workspace-write mode makes `.git` read-only, so it cannot commit); Claude runs with an allowlist.
- The PR title check of a repository must accept the Conventional Commits form above. Repositories with other rules need a per-repo setting (not built yet).
- A pull request is read through the GitHub CLI of the machine that runs the gate; `gh` must be logged in as the account that owns the pull requests.
