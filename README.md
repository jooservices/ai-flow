# ai-flow

Jira-driven multi-AI delivery flow for Workspaces (Jira project `JAIF`). Supervisors on the Mac and the WSL workstation poll Jira, start a developer AI for each ready ticket, then a reviewer AI and a QA AI, merge and move the ticket to Done. The owner works only in Backlog.

## Flow

| Jira status | Who | Next |
|---|---|---|
| Backlog | owner / any AI creates and refines | owner or AI moves to **To Do** when the ticket is complete (that means "ready") |
| To Do | supervisor claims it (Redis lease), moves to In Progress | developer AI (the `Agent (AI)` field) works in a ticket worktree |
| In Progress | developer AI; no lease for `AIFLOW_ORPHAN_GRACE_SECONDS` means the AI died, so another AI continues | `RESULT: DONE` with commits moves to In Review |
| In Review | reviewer AI (never the developer) | `VERDICT: PASS` moves to In QA; `FAIL` moves back to In Progress |
| In QA | QA AI (never the developer, not the reviewer when possible) | `PASS` merges the branch into the base and moves to Done; `FAIL` moves back to In Progress |
| Blocked | needs the owner | too many failed attempts or `RESULT: BLOCKED` |

The ticket may name `Repo: <name>` (under `JOOservices/projects`) and `Base: <branch>`; defaults come from `.env`. Tickets with unfinished "is blocked by" links are skipped. A ticket with label `aiflow-hold` is never picked up.

## Hand-over

Every AI comments on the ticket (format in `prompts/common.md`). The supervisor also comments at start, end, failure and quota events. Activity (log output or a new commit) is watched; no activity for `AIFLOW_IDLE_MINUTES` kills the AI and the ticket is reassigned. A usage-limit error puts that AI on a cooldown (`AIFLOW_QUOTA_COOLDOWN_MINUTES`) and the next AI continues from the branch and the comments.

## Run

```
cp .env.example .env              # set AIFLOW_MACHINE (mac|wsl) and AIFLOW_SLOT
bin/aiflow-supervisor --once      # one pass (exit 3 when nothing was ready)
bin/aiflow-supervisor             # loop
python3 -m unittest discover -s tests -t .
```

State lives in the shared Redis (database from `scripts/docker/shared provision-project ai-flow --redis`, `REDIS_DB`), keys under `aiflow:` (lease, attempts, cooldown, dev, reviewer, home, heartbeat). Logs: `Workspaces/.tmp/aiflow/logs/`.

## Known limits (v1)

- Developer AIs run with broad tool access inside the ticket worktree and a deny list for destructive commands; a narrower allowlist is JAIF-7.
- A repo without a remote can only continue on the machine that holds the worktree (`home:<KEY>`). Cross-machine continuation needs a pushed branch and a fetch-based checkout (JAIF-3).
- One Redis: the supervisor on the Mac needs a tunnel to the WSL Redis (JAIF-8).
