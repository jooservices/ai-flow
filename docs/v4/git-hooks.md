# Git hooks: fast checks at commit, affected tests at push

The hooks run on the developer's machine and spend no AI tokens. Their output is a few lines, so an AI that reads a refusal
spends little. The code is `core/git_hooks.py` (the hook checks) and `core/local_gate.py` (`install-hooks`, the step runner).

Install once per clone with `python3 -m aiflow.v4.core.local_gate install-hooks`. It verifies that `pre-commit`,
`commit-msg` and `pre-push` exist and are readable, then sets `core.hooksPath` to `.githooks` and makes the hook files
executable. If the clone already uses a different `core.hooksPath`, the installer warns and refuses to replace it unless
invoked with `--force`. The setting is shared by linked worktrees, but hook permissions are repaired per worktree; run
the installer in each affected linked worktree too.

## pre-commit (`.githooks/pre-commit`)

Steps, cheapest first; the first failure stops the run:

| Step | Checks | Tool |
| --- | --- | --- |
| `whitespace` | trailing whitespace, conflict markers in the staged change | `git diff --cached --check` |
| `lint` | staged modules under `aiflow/v4` (only when one is staged), read from the Git index | `ruff check --stdin-filename` with the project rules |
| `secrets` | secrets in the staged change; fake test credentials are allowed in `deploy/v4/ci/gitleaks.toml` | `gitleaks` |
| `giant-files`, `duplicate-blocks`, `broad-except`, `sql-boundary` | the project guards, as in CI | `aiflow.v4.core.*` |

The commit-message rules are not a pre-commit step: the `commit-msg` hook checks them (`core/commit_rules.py`).

- All four project guards read the staged snapshot commit. `duplicate-blocks` and `sql-boundary` use a temporary worktree
  of that snapshot, including its baselines, so unstaged edits cannot change their result.
- A refusal prints the summary of the steps and the last 20 lines of the failing step. Fix the problem and commit again.
- `gitleaks` and `deploy/v4/ci/gitleaks.toml` must both be available. A missing allowlist refuses the commit and names
  the file; a missing tool refuses the commit with the install hint (free: https://github.com/gitleaks/gitleaks).

## pre-push (`.githooks/pre-push`)

1. Refuses a push with uncommitted changes, or a ref that is not the checked-out commit (the gate checks the checkout).
2. `smoke`: `tests/v4/test_architecture.py` and `tests/v4/test_feature_structure.py`.
3. `affected-tests`: the v4 test files changed between each pushed ref's destination SHA and local SHA, as reported by
   Git on pre-push stdin. A new remote ref uses the merge base with `origin/develop`. A changed test file runs itself; a
   changed module `aiflow/v4/<...>/<name>.py` runs `tests/v4/test_<name>.py`; a hook change runs
   `tests/v4/test_git_hooks.py`. A module without such a test file is printed as "CI runs them". The full v4 suite stays in CI.

The affected tests need the test services (`AIFLOW4_TEST_REDIS_URL`, `AIFLOW4_TEST_DATABASE_URL`). Without them the push
exits 2 and is refused, so the missing service is seen before the push.

## Skipping a check

`AIFLOW_HOOK_SKIP=secrets,lint git commit ...` leaves the named checks out of pre-commit. The output names the skipped checks.
`git commit --no-verify` skips every hook. CI runs the same checks again, so a skip is never the last check.

## Adding a check

Add a `Step` to `pre_commit_plan` or `pre_push_plan` in `core/git_hooks.py`, with a name, its command and a time limit. Keep
the calls visible in that function: no dispatch through a dictionary or a loop over functions. Give a new function a unit test
in `tests/v4/test_git_hooks.py`.
