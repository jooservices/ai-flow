# Browser journeys path filter

The browser journeys (`Test (v4) browser journeys` in `.github/workflows/ci.yml`) run only when a pull request changes the console or a browser test. The job `changes` compares the pull request with its base commit and sets the output `browser`. The journeys job waits for it and is skipped when the output is `false`. A push or a merge-queue run always runs the journeys.

The filter matches these paths:

- `aiflow/v4/control/ui/` (the console files)
- `tests/v4/test_console_browser.py` and `tests/v4/test_log_viewer_browser.py`
- `tests/v4/console_browser_suite.cjs` and `tests/v4/log_viewer_browser_suite.cjs`

A change to the workflow file itself does not match, so a change to the journeys job is checked by the next pull request that touches the console or a browser test. If the diff cannot be read, `changes` fails and the required check fails; it never skips the journeys silently.

The required check `Test (v4)` passes when `changes` and every shard passed and the browser journeys passed, or were skipped because the filter said no browser change. A browser journeys failure on a run that needed them still blocks the merge.

To add a path, add one alternative to the `grep -qE` pattern in the `Does the change touch the console or a browser test?` step of `changes`, and add the file to the list above. `tests/v4/test_browser_path_filter.py` runs the filter step on a throwaway repository and pins which paths match, and it runs the aggregate step with each combination of job results.

## Routes

None; this is a CI change.
