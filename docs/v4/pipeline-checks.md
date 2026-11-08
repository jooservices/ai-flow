# Pipeline checks and first-run failures

**Console screens:** `cc`, `work`, `delivery`, `projects`

Projects configured with ``local_gate: ai-flow`` run the CI checks that work in an AI run environment before a developer
opens a pull request. Verification repeats that gate on the PR worktree before it accepts a passing review. Both AI
prompts list every configured project command and require a result for each command.

`aiflow/v4/core/pipeline_checks.py` classifies each workflow job as one of:

- **Project check:** the job is covered by named steps in `aiflow/v4/core/local_gate.py`.
- **Pipeline only:** the run needs infrastructure the worker does not promise, such as Docker, a provisioned browser, or
  GitHub issue-write permission. The catalog gives the reason shown on the Projects screen.

The Projects screen shows configured project commands, locally checked CI jobs, and pipeline-only jobs with their reasons.
When verification sees a failed CI job outside the local gate, it includes a check-gap finding in the Jira and pull-request
rework report. The Dashboard shows the first CI result recorded by verification for each pull request, grouped by UTC day,
with failed job names and a target share below 15 percent.

To add or change a job, update the GitHub workflow, add its job key and either its local gate step names or a pipeline-only
reason to `PIPELINE_JOBS`, then update `local_gate.steps()` for a runnable check. The parity test compares every workflow job
with this catalog and checks that each named local step exists. Keep multi-job checks in CI order and give every pipeline-only
entry a concrete environment reason.

The Dashboard metric uses the earliest verify run with a non-empty CI status snapshot for each pull-request URL. That snapshot
is already stored in the run result, so this feature adds no table or data-retention policy.

## Routes

- `GET /v1/dashboard/pipeline-failures` Returns first observed verification CI results per pull request for the caller's visible projects over the requested number of days.
- `GET /v1/projects` Lists projects visible to the caller, including their configured checks and machine readiness.

## Known limits

- A check gap is classified from the project's configuration and the catalog, not from the outcome of each local gate step.
  If the test-service variables are missing the local gate skips the `v4` step while verification continues, so a failed
  matching CI shard can be reported without a gap; the run record (`details.local_gate`) already carries each step's
  status and is the data a later change should classify from.
- The Dashboard polling line is matched by a pattern that also covers a trailing comment; a change to that line asks for
  the Work and Delivery pages although the call only runs on the Command Center. This only widens the documentation check.
