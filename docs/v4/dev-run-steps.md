# Developer run steps

`run_dev` calls five named steps in order: `prepare`, `brief`, `run_ai`, `collect`, and `publish`. The same fenced
run setup, Jira reporter, environment resolver, worktree and account paths, provider request, adapter setup, runtime
selection, node label, and heartbeat cleanup are shared with verification through `worker/run_steps.py`.

## Steps

- **prepare** gets the scoped GitHub token, mirror and ticket worktree; it checks for a worktree from another
  repository, merges the latest base when continuing a branch, resolves the dev environment, updates Jira to In
  Progress, and records the started note. A rework run also fetches the failing CI log through the control API.
- **brief** creates the run log directory, resolves the environment again, assembles the prompt and provider request,
  and selects the adapter and runtime.
- **run_ai** starts a configured WordPress test site inside the runtime when needed, records its ready note, and runs
  the adapter. Heartbeat progress and inbox acknowledgements continue at provider process boundaries.
- **collect** records provider usage and verdict details, parses the summary, tests and handover, handles quota,
  missing environment or tools, and blocked outcomes, then records changed files. Timeout handover runs before
  unresolved merge markers are classified.
- **publish** handles no-change and failed runs, or validates the commit title, commits, runs the optional local gate,
  pushes, opens a draft PR and reports it to Jira.

## Observable effect order

1. It constructs the Git client, starts the heartbeat, records `t0`, then enters the exception handler. The
   heartbeat thread can issue `POST /v1/runs/{run_id}/heartbeat` throughout the run; process-boundary inbox checks
   use that same endpoint.
2. It requests `POST /v1/github/token`, then runs Git mirror, foreign-worktree, worktree, ahead-count and optional
   base-merge operations in that order. A foreign worktree adds a warning log, `POST /v1/jira/{key}/comment`, then
   Git worktree removal before normal setup continues.
3. It resolves the dev environment, then sends Jira `transition` to In Progress, `fields`, and the started `comment`.
   Rework runs then request `GET /v1/github/ci-log`. It creates the log directory, resolves the environment a second
   time, and assembles the prompt and provider request.
4. It enters the selected runtime. A WordPress run starts its site and posts a ready `comment`; then the provider
   adapter runs as a subprocess. Heartbeat progress and inbox acknowledgements may interleave with this work.
5. On return, it records provider usage and result details before checking lease loss and parsing summary, tests and
   handover. Quota, missing-environment and missing-tool paths comment and transition to To Do; a blocked path
   comments and transitions to Needs owner. Missing-tool and blocked paths also call `POST /v1/escalations/raise` as
   applicable. Otherwise it asks Git for changed files, attempts timeout handover, and classifies unresolved conflict
   markers. Timeout handover checks changed files again, then `GET /v1/github/pr`; when there is work to keep it
   commits if needed, requests a branch token, pushes and resolves HEAD, opens or reuses a PR, comments and
   transitions to In Verify.
6. A clean no-change run requests `GET /v1/github/pr`. If committed branch work exists, it can then request a
   branch-scoped token, push, resolves HEAD and `POST /v1/github/pr`; it reports the result with a Jira comment and
   transition to In Verify. A no-work result instead comments and transitions to To Do.
7. A changed run validates the commit title, runs Git commit, and optionally runs the local gate. A failed gate keeps
   the failed work, soft-resets the commit, then comments and transitions to To Do. A passing path requests a
   branch-scoped token, pushes, posts the draft PR, comments, then transitions to In Verify.

The worker writes no database rows directly. Jira writes and their event and audit effects pass through the fenced
control API calls above; Git and adapter operations are subprocess effects. Existing lost-lease and fencing outcomes
return without a new Jira report. A generic worker exception keeps the `dev step failed` log, attempts its existing
Jira comment and To Do transition, and suppresses errors from that best-effort report. The heartbeat stops in
`finally` on every handled path.

## Adding a step

Keep each step at or below 80 lines and McCabe 12. Pass its inputs through a small run dataclass or explicit
parameters, call steps explicitly from the orchestrator, and preserve the current API, Git, heartbeat and Jira effect
order. Put a helper in `run_steps.py` only when developer and verification behavior is identical or differs by an
explicit parameter.
