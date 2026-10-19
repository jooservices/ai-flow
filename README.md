# ai-flow

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
