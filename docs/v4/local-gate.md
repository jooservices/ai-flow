# Local gate: the CI checks, run before anything is pushed

`python3 -m aiflow.v4.core.local_gate run --path <worktree> --base develop [--title "<pull request title>"] [--worktree]`
runs the commands of the CI jobs `Test (Unit)`, `Giant files`, `Documentation` and `Test (v4)` (the scale proof first, as CI
does) and the header rules of the commit and the pull request title (`core/commit_rules.py`). `local_gate steps` lists them.

- **Exit codes:** 0 every step passed; 1 a step failed (the run stops there and prints the tail of its output); 2 nothing
  failed, but a step did not finish in its time slice or needs a service variable that is not provided.
- **`--worktree`** skips the three steps that compare the committed HEAD with the base. The developer AI runs it before it
  finishes; its prompt lists the command in the checks.
- **Load-sensitive steps:** the scale proof measures wall-clock budgets, so the gate runs it once more before it reports a failure.
- **Parity:** `tests/v4/test_local_gate.py` reads `.github/workflows/ci.yml` and fails when a check command of those jobs is
  not a gate step. Change the workflow and the gate together.
- **In the developer run:** a project opts in with `local_gate: ai-flow` (or a list of commands) in its control-repo file.
  AI Flow commits locally, runs the whole gate on that commit and pushes only when no step failed. A failing step ends the run
  as `failed` / `gate_failed`, the commit is undone, the Jira comment names the step and the tail of its output, and nothing is
  pushed. A step that did not finish is listed in the pull request body under "Local gate", so CI completes it.

`POST /v1/work/claim` returns the project's `local_gate` setting to the worker, so the worker knows whether to run the gate.

## Git hooks

`python3 -m aiflow.v4.core.local_gate install-hooks` (once per clone) points `core.hooksPath` at `.githooks`:

- `commit-msg` rejects a message that breaks the commitlint rules (`core/commit_rules.py`) when you commit, so the error is
  found in the editor and not after the push or in CI.
- `pre-push` runs the whole gate, the same checks as CI, before anything is pushed.

## Limits worth knowing

- A step that fails after the AI finished ends the run as `gate_failed` (or `gate_error` when the gate itself could not run). The
  commit is undone and the failed work is saved as `<run log>.gate-failed.patch` next to the run log, because the next claim
  cleans the worktree. Restoring that work into the next run, and letting the AI repair a gate failure inside the same run, are
  follow-up work (JAIF-430).
- The merge commit that brings the base into an existing branch is pushed without the gate (JAIF-430).
