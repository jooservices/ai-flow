# Delivery metrics and the refactor baseline

`uv run python -m aiflow.v4.core.delivery_metrics [--limit 100]` reads the last merged pull requests of `develop` through
`gh` and prints four measures of how hard it is to change this code: lead time (opened to merged), branch-update commits
(a pull request that had to take `develop` again, which is what a conflict or a lost race looks like), CI attempts per
pull request (workflow runs plus re-runs), and how many pull requests touch the hot files `control/api.py` and
`control/ui/console.html`. The calculation is pure and tested; only the fetch calls `gh`.

## Baseline before the refactor (epic JAIF-445), 29 October 2026, last 100 merged pull requests

| Metric | Value |
| --- | --- |
| Lead time, median (hours) | 2.19 |
| Lead time, p90 (hours) | 81.1 |
| Pull requests with a branch-update commit | 55 |
| Branch-update commits per pull request | 1.15 |
| CI attempts per pull request | 3.89 |
| Pull requests touching `aiflow/v4/control/api.py` | 50 (50%) |
| Pull requests touching `aiflow/v4/control/ui/console.html` | 35 (35%) |

## How it is used

After phase 2 of the refactor (the pilot features) the command is run again and compared with this table before the
roll-out continues (JAIF-473). The structure is working when pull requests touching `api.py` fall well below half and
branch-update commits and CI attempts per pull request fall. If they do not, the roll-out stops and the plan is revised.
The weekly report (`notify.py`) does not include these numbers yet; run the command for the review.
