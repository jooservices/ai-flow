# Developer run limits

AI Flow estimates each Intake ticket from its size, completed ticket history and a conservative baseline. The estimate
is included in the intake prompt. When its upper time estimate exceeds the 30-minute target, Intake must return an
ordered split plan of two to eight independently testable Stories. Control saves the plan, creates or resumes children
with stable markers, links each child to the parent and the next child to its prerequisite, then makes the complete
child set ready. Every child must have an estimated upper work time no greater than 30 minutes before any child is
created or offered. Applicable parent labels such as `ai-*`, `tier-*`, `area-*`, and `security` are copied to each
child; readiness, split markers, and parent size labels are not. The parent moves to Backlog and loses its ready label.
A plan retry resumes the existing children.

Developer prompts ask the AI to run focused checks for the change first and the configured full suite once at the end,
reserving the last 15 minutes for that suite. They require a machine-readable full-suite result. The worker records a
missing completion as unfinished. If the hard time limit interrupts a run that has changes, the worker preserves and
pushes those changes, opens a draft pull request, moves the ticket to In Verify and marks the suite as unfinished so
the pull request pipeline must complete its checks. A timed-out run with no changes returns to the normal retry path.
Each completed timeout increases that ticket's next developer hard limit by 30 minutes; the increment is cumulative.
The effective target, warning and hard limits are stored with the run, and the worker, watchdog, master clock and
`restart_run` use those same per-run limits. `GET /v1/master/context` reports the effective run clock, and
`POST /v1/master/actions` applies the same limit when it restarts a run.

The Dashboard's Developer run timeouts block shows the developer-run timeout share for the last 30 UTC days, a daily
breakdown, and tickets with timeouts. It uses only ended developer runs and scopes results to the signed-in user's
projects. The target indicator is below 10 percent.

The split is submitted through `POST /v1/jira/{key}/split`, work is claimed through `POST /v1/work/claim`, and the
Dashboard reads `GET /v1/dashboard/timeouts`. The Command Center Dashboard (`cc`) displays the daily and per-ticket
counts, the developer-run timeout share and the below-10-percent target.

**Console screens:** `cc`

Runs continue to use the machine CPU and memory limits already applied to node workers by the JAIF-268 node slice.
This feature does not change those machine limits or add dependencies. The full v4 pytest suite remains the merge
gate, including after the worker hands an unfinished suite to the pull request pipeline.
