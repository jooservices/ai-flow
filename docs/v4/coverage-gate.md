# Coverage gate, Codecov and README badges

Pull requests that change Python or tests run a **Coverage** job in `.github/workflows/ci.yml` in parallel with the
other checks. The job is path-filtered, times out at seven minutes, and is not a dependency of the required
`Test (v4)` aggregator.

## Steps

1. **Path filter** — skip when the diff against the pull-request base (or `origin/develop`) does not touch `aiflow/`,
   `tests/`, `pyproject.toml`, `uv.lock` or `.github/workflows/ci.yml`.
2. **Install** — `uv sync --frozen --group dev`, then the CI-only packages `pytest-cov` and `diff-cover` (pinned in the
   workflow; not project dependencies).
3. **Tests with coverage** — run the focused paths from `aiflow.v4.core.affected_tests` when the map is certain. When
   that map falls back to the full suite, `deploy/v4/ci/coverage_select_tests.py` keeps the run small (smoke tests plus
   stem-matched modules). Coverage is written to `coverage.xml` for `aiflow`.
4. **Diff coverage** — on pull requests and the merge queue, `diff-cover coverage.xml --fail-under=85` fails when
   changed lines are under 85 percent covered.
5. **Codecov upload** — `codecov/codecov-action` uploads `coverage.xml` (free for a public repository). The action is
   pinned to a commit SHA with a version comment, like the other workflows.

## README badges

The repository README shows CI, Coverage (Codecov), CodeQL, OpenSSF Scorecard, Python 3.12+, GitHub Release and License
badges. They are status links only; they do not change runtime behaviour.

## Routes

None; this is a CI and documentation topic.

## How to extend

- Raise or lower the diff threshold only with a ticket that updates the `diff-cover --fail-under` value and this page.
- Keep coverage tools out of `pyproject.toml` unless a ticket makes them a local developer dependency.
- Classify any new workflow job in `aiflow/v4/core/pipeline_checks.py` so the Projects screen stays accurate.
