# Post-merge verification

The workflow `.github/workflows/ci-post-merge.yml` re-verifies `develop` and `master` after every push to them. It is not part of the pull request pipeline and not a required check, so it never blocks a merge; a failure is reported as a bug ticket instead.

The steps:

- `Post-merge unit tests`: compiles `aiflow` and runs the unit tests of `tests/`, as the `Test (Unit)` job of `ci.yml` does.
- `Post-merge v4 shard 1` to `4`: each runs one balanced shard of the v4 tests against its own Redis and PostgreSQL, with the same shard command as `ci.yml` (`test_shards files`).
- `Report a post-merge failure`: when any job above failed, opens a GitHub issue labelled `bug` with the branch, the commit and the link to the run.

A newer push to the same branch cancels the run in progress. `ci.yml` is not changed: its jobs are copied here because a reusable workflow would need a change to `ci.yml`. To add a check, add a job to `ci-post-merge.yml`, list it in `needs` of the report job and pin it in `tests/v4/test_post_merge_workflow.py`. Keep the shard count equal to `ci.yml`; a test compares them.

## Routes

None; this is a CI change.
