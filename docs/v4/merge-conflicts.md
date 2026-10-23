# Fewer merge conflicts

**Console screens:** Delivery (`delivery`).

Many tickets run at once and most touch the same few files, so branches conflicted with the base and went back for
another development run and a new verification. Five measures keep that work from being lost.

## Hot-file scheduling (`aiflow/v4/control/hotfiles.py`)

Before a dev run is offered, AI Flow predicts the files the ticket will touch:

| Source | What is read |
|---|---|
| ticket | file paths written in the summary and description, and keyword areas (`console`, `dashboard`, `control api`, `endpoint`, `dependencies`, `changelog`) |
| epic | the same for the epic's text |
| history | `changed_files` of the last finished dev runs of the same epic (and of the ticket itself) |

A predicted file is *hot* when it matches `HOT_FILES` (the console page, `aiflow/v4/control/api.py`,
`tests/v4/conftest.py`, `pyproject.toml`, `uv.lock`, `CHANGELOG.md`). A ticket is **not offered** while another ticket
shares a hot file with it and has a running dev run or an open pull request (In Verify, Human review). The account is
offered another ticket instead, or waits. The ticket starts when the other one merges, or after it has waited six
hours (`MAX_WAIT`), so a hot file never starves a ticket.

The wait counts from the moment the ticket was first held back (table `hot_file_blocked`, migration
`019_hot_file_blocked.sql`), not from the age of the ticket: an old backlog ticket is still held on its first check.
The record is removed when nothing blocks the ticket any more, so a later block starts a new wait. The ticket list
`POST /v1/work/claim` accepts and `GET /v1/work/candidates` offers follow this rule.

## Automatic branch update (`aiflow/v4/worker/verify.py`)

A verify run that finds the pull request behind the base asks GitHub to merge the base in. A clean merge is done by AI
Flow (`details.auto_updated`), the verify worktree moves to the new head and CI runs on it. Only a real conflict, which
GitHub refuses, is sent back to a developer AI: outcome `rework`, reason "conflicts with <base>".

## One file per topic

The "Topics" list of `deploy/v4/README.md` was appended to by every ticket. Now a topic is a file
`deploy/v4/topics/<topic>.md` with exactly one line:

```
- [Title](../../../docs/v4/<topic>.md): what it covers
```

The index is generated from these files and the older README lines: `uv run python -m aiflow.v4.core.topics`.
Do not edit the README list.

## Merge queue (`aiflow/v4/control/merge_queue.py`)

`POST /v1/github/merge` from a verify run joins the queue of the repository (table `merge_queue`). Only the oldest
waiting pull request has the turn; the others get `queued` with their position and the verify run polls (its lease
heartbeat keeps running, up to 45 minutes). On its turn the pull request is brought up to date with the base, which
includes the pull request merged just before it; CI runs on the new head, then it merges. A conflict at that point is
a real conflict and goes to rework. An entry whose run ended, or that was silent for 20 minutes, is dropped.

## Command Center: work lost to conflicts

The Analytics panel shows, for the last 14 days and the 14 before, with a per-day trend (`GET
/v1/analytics/conflicts?days=14&bucket=day|week`; it sits beside the per-account runs of `GET /v1/analytics/daily`
and has the same access rule, `view.dashboard`):

- **Share lost to conflicts**: verifications with outcome `rework` and reason "conflicts with <base>" divided by all
  completed verifications (merged, handed to a person, reworked, failed). A verification that failed for a conflict
  and something else counts once, as a conflict, because verify stops at the conflict.
- **Time spent**: the run time of those verifications, the dev rework runs they triggered and the re-verifications
  that followed (each run counted once); total and average per conflict.
- Branches AI Flow updated itself with a clean merge are not counted; their number is shown beside the share.

The definition is printed under the numbers in the console.
