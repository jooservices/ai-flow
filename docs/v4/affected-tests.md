# Affected test selection

The developer helper maps reviewed v4 source files to their focused test modules and always adds the install and route registry smoke checks. A change in shared core code, migrations, an unreviewed source file, a missing mapped test, or an empty change set selects every `tests/v4` module. The documentation builder and this developer-only selector have exact test mappings because neither changes runtime behavior.

Run `uv run --frozen python -m aiflow.v4.core.affected_tests --base origin/develop` to select and run tests for a local change. The report gives selected and omitted test file counts and elapsed time. A successful full-suite run records its duration in `.pytest_cache`; later focused runs report time saved against that baseline. `--full-suite-seconds <seconds>` supplies a baseline manually, and `--plan-only` prints the selection without running it. The runner requires `AIFLOW4_TEST_DATABASE_URL` to use the project's `_test` role and test database and requires `AIFLOW4_TEST_REDIS_URL`. CI runs the full v4 shard and browser suite on pull requests; its scheduled job runs the full clean-install acceptance check.

To add a mapping, add the exact source path and all tests that cover it to `FILE_TESTS`, then add a unit test that pins the selection. Keep shared modules out of the map: they use the full-suite fallback. Changes under `tests/v4` select the changed test module directly.

## Routes

None; this is a developer test helper.
