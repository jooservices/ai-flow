# Local gate: a green run before review

The developer's pull request is opened only after every check in the selected local plan passes. The plan comes from
`local_gate: ai-flow` (AI Flow's built-in CI steps), a `local_gate` list of commands, or the project's `checks` list,
in that order. A project without any configured checks has no green result and cannot be submitted for review.

For AI Flow's built-in plan, `python3 -m aiflow.v4.core.local_gate run --path <worktree> --base develop
[--title "<pull request title>"] [--worktree]` runs the CI commands and commit and pull request title rules.
`local_gate steps` lists the plan. Exit code 0 means every step passed; 1 means a step failed; 2 means a step timed
out or needs a service variable that is not provided. `--worktree` skips checks that compare committed HEAD with the
base. The scale proof is retried once because its wall-clock budget is load-sensitive.

Every gate step must finish successfully before a PR is opened. A failure, timeout, missing environment item, missing
check plan, or gate error ends the dev run as failed, saves the work patch, and sends the ticket back without review.
The run record stores `details.local_gate` with an overall `green`, `red`, `incomplete`, `missing` or `error` status
and each executed step's name and status. Verification dispatch requires the newest dev run to be finished, successful,
attached to a PR, and recorded as green. It does not fall back to an older green run while a newer dev run is active or
failed. The account and ticket are already recorded on the run, so later metrics can measure first-submit pass rates.

Project `checks` are set in the existing **Checks** field on the Projects screen. The worker receives the project's
`local_gate` and `checks` settings in its work claim, runs the selected commands after the local commit, and stores
only step names and statuses in the run record. The result is sent through the existing run result endpoint.

## Routes

- `GET /v1/work/candidates` offers verification only for a latest finished dev run with a pull request and a green local gate.
- `POST /v1/work/claim` refuses verification claims when the latest dev run has no green local gate result.
- `POST /v1/runs/{run_id}/result` records the worker's local check result in the run details.

## Git hooks

`python3 -m aiflow.v4.core.local_gate install-hooks` (once per clone) checks that the `pre-commit`, `commit-msg` and
`pre-push` hook files exist and are readable, then points `core.hooksPath` at `.githooks` (details:
`docs/v4/git-hooks.md`). If the clone already uses a different `core.hooksPath`, the installer warns and refuses to
replace it unless invoked with `--force`. The setting is shared by linked worktrees, but permissions are repaired per
worktree; run the installer in each affected linked worktree too.

- `commit-msg` rejects a message that breaks the commitlint rules (`core/commit_rules.py`) when you commit, so the error is
  found in the editor and not after the push or in CI.
- `pre-push` runs the tests of the v4 files the push changes, plus the smoke tests (`docs/v4/git-hooks.md`). The whole gate
  runs in the developer run and in CI.
- `pre-commit` runs the fast checks of the staged change (`docs/v4/git-hooks.md`).

## Adding a check

Use the project's **Checks** field for its normal test commands, `local_gate: ai-flow` for AI Flow's CI suite, or a
`local_gate` command list for a project-specific replacement plan. Keep each command runnable from the repository root
with the project's configured test environment. The worker records one name and status for each step; no command
invocation or credentials are stored in the run record. A failed command's bounded output remains in the existing run
summary and ticket comment. `tests/v4/test_local_gate.py` compares AI Flow's built-in plan with `.github/workflows/ci.yml`
so CI changes and the local plan stay aligned.

## Limits

Failed work is saved as `<run log>.gate-failed.patch` because a later claim cleans the worktree. The worker does not
repair gate failures in the same run. A provider timeout still follows the existing pipeline hand-over path and records
the local gate as incomplete; verification dispatch refuses that run. The merge commit that brings the base into an
existing branch is pushed without the gate (JAIF-430).
