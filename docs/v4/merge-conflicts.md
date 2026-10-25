# Fewer merge conflicts

**Console screens:** Delivery (`delivery`) and Command Center (`cc`).

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
shares a hot file with it and has a running dev run or is waiting in In Verify, Human review or Ready to merge. A ticket sent back for rework with an open pull request holds nothing while it waits; it updates its branch when it resumes. The account is
offered another ticket instead, or waits. It remains blocked for as long as the holder is running or waiting in
review; elapsed time never allows overlapping tickets on a hot file.

The wait counts from the moment the ticket was first held back (table `hot_file_blocked`, migration
`019_hot_file_blocked.sql`), not from the age of the ticket: an old backlog ticket is still held on its first check.
The record is removed when nothing blocks the ticket any more, so a later block starts a new wait. The ticket list
`POST /v1/work/claim` accepts and `GET /v1/work/candidates` offers follow this rule.

The check when candidates are listed is a snapshot, so the claim reserves the files again: after the per-ticket lease
is taken, `hotfiles.reserve` writes one row per predicted hot file into `hot_file_reservations` (migration
`020_hot_file_reservations.sql`), in one transaction under an advisory lock. Of two simultaneous claims for different
tickets only the first gets a shared file; the second is answered `409` ("hot file ... is reserved by <ticket>") and
its lease is released. A reservation is dropped as stale when its run is no longer running and its ticket has no open
pull request (In Verify, Human review, Ready to merge) or its pull request remains open. A ticket that waited longer still waits for the holder to release its
reservation.

## Automatic branch update (`aiflow/v4/worker/verify.py`)

A verify run that finds the pull request behind the base asks GitHub to merge the base in. A clean merge is done by AI
Flow (`details.auto_updated`), the verify worktree moves to the new head and CI runs on it. Only a real conflict, which
GitHub refuses, is sent back to a developer AI: outcome `rework`, reason "conflicts with <base>". GitHub's
update-branch answer is classified: only a `422` that names a merge conflict is a conflict. A permission error or a
service error (`401`, `403`, `404`, `5xx`) is reported as `update_failed`, leaves the queue and is not sent to a
developer AI.

## One file per topic

The "Topics" list of `deploy/v4/README.md` was appended to by every ticket. Now a topic is a file
`deploy/v4/topics/<topic>.md` with exactly one line:

```
- [Title](../../../docs/v4/<topic>.md): what it covers
```

The index is generated from these files and the older README lines: `uv run python -m aiflow.v4.core.topics`.
Do not edit the README list.

## Merge queue (`aiflow/v4/control/merge_queue.py`)

`POST /v1/github/merge` from a verify run and the background Ready to merge loop join the same repository queue
(table `merge_queue`). Only the oldest
waiting pull request has the turn; the others get `queued` with their position and the verify run polls (its lease
heartbeat keeps running, up to 45 minutes). On its turn the pull request is brought up to date with the base, which
includes the pull request merged just before it; CI runs on the new head, then it merges. A conflict at that point is
a real conflict and goes to rework. An entry whose run ended, or that was silent for 20 minutes, is dropped.

The turn, the branch update and the merge run under a per-repository lock (a PostgreSQL session advisory lock on its
own connection, `merge_queue.repo_lock`). A request that finds the lock taken gets `queued` and asks again, so two
concurrent requests never merge at once; the lock is released when the merge ends, raises or the control process dies.

Human-review approval (`POST /v1/reviews/{key}/approve`) and skip-gate use the same queue: the click waits up to 90
seconds for its turn. When the branch had to be brought up to date first, or the turn did not come, nothing is merged
on that click and the reviewer approves again.

## Routes

The claim reservation is in `POST /v1/work/claim`; the queue is behind `POST /v1/github/merge`,
`POST /v1/reviews/{key}/approve` and `POST /v1/reviews/{key}/skip-gate`. The control API's
`POST /v1/github/token`, `GET /v1/github/git/{git_path:path}`, `POST /v1/github/git/{git_path:path}` and
`GET /v1/state` and `GET /v1/reviews` are unchanged by this work.

## Command Center: work lost to conflicts

The Delivery Analytics panel and the Command Center dashboard show, for the last 14 days and the 14 before, with a per-day trend (`GET
/v1/analytics/conflicts?days=14&bucket=day|week`; it sits beside the per-account runs of `GET /v1/analytics/daily`
and has the same access rule, `view.dashboard`):

- **Share lost to conflicts**: verifications with outcome `rework` and reason "conflicts with <base>" divided by all
  completed verifications (merged, handed to a person, reworked, failed). A verification that failed for a conflict
  and something else counts once, as a conflict, because verify stops at the conflict.
- **Time spent**: the run time of those verifications, the dev rework runs they triggered and the re-verifications
  that followed (each run counted once); total and average per conflict.
- Branches AI Flow updated itself with a clean merge are not counted; their number is shown beside the share.

The definition is printed under the numbers in the console.

## Tickets per hot file

`AIFLOW4_HOT_FILE_SLOTS` (default 1) sets how many tickets may work on the same hot file at the same time. With 1 the flow never starts two tickets on the same hot file, which avoids conflicts but runs those tickets one after another. With 2 or 3 more tickets run at once; their branches are updated with develop before verification. The setting is read at start by the control services; the reservation table keeps one row for each file and ticket.

