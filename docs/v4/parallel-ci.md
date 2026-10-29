# Parallel CI

A complete CI run used to take about 16 minutes, almost all of it one job (`Test (v4)`), and the strict branch protection
runs that job again for every open pull request after each merge. The v4 suite now runs on parallel runners
(JAIF-431, JAIF-432, JAIF-433).

| Job | What it runs | Required |
|---|---|---|
| `Test (v4) shard 1..4` | the test files of one shard, each runner with its own Redis and PostgreSQL | through `Test (v4)` |
| `Test (v4) browser journeys` | `test_console_browser.py` and `test_log_viewer_browser.py` (Playwright) | through `Test (v4)` |
| `Test (v4)` | passes only when every shard and the browser journeys passed | yes |
| `Scale proof (not required)` | `tests/v4/test_scale_proof.py` against a fresh PostgreSQL service | no |

The required check keeps its old name, so branch protection does not change.

## Shards

`python3 -m aiflow.v4.core.test_shards files --index <1..4> --total 4` prints the files of one shard. Files are assigned
largest first to the lightest shard, so the partition is the same on every runner. `tests/v4/test_test_shards.py` fails
when a test file is in no shard (and has no job of its own) or in two shards, so a new test file is picked up
automatically. When the shard count changes, change the matrix in `.github/workflows/ci.yml` and `SHARDS` together.

## Scale proof

The 100/50/200 benchmark depends on the speed of the runner (the console page-load budget has failed at 2.3 s against a
limit of 2 s), so it no longer blocks unrelated pull requests. It still runs on every pull request, and the local gate
still runs it (once more on failure). The budgets are not loosened; the slow call is tracked in JAIF-436.

## Cache

The jobs that install the dependencies (Documentation, Code quality, the shards, the browser journeys and the scale
proof) restore `~/.cache/uv` before the install step, keyed by `uv.lock`. A cold cache still works: no job depends on a
hit (JAIF-434).
