# Silent developer runs

AI developer runs sometimes ended turns promising to report later, timing out while running tests or commands
in the background, or completing without an actual verdict. When these runs were marked failed or lost, their
tokens were wasted and the run could not finish its work.

## Prompt instructions and foreground execution

The developer instructions in `prompts/v4/dev.md` forbid ending a turn with a promise to report later. When
running covering tests or long test suites, the AI executes commands in the foreground using the allocated
time slice (`FULL_SUITE_TIME_SLICE_MINUTES`) rather than backgrounding processes. The AI reports only finished
results with concrete test outcomes.

## Resumption on silence

When a developer run finishes an initial turn with a silent response (such as promising to report later,
timing out without output, or returning an empty summary), the worker does not immediately discard the run.
Instead, it resumes the session once with an explicit instruction to continue, wait for test execution, and
provide the final report.

## Termination on second silence

If the resumed run goes silent a second time, the run is terminated with outcome `failed`, `error_kind="silent"`,
and summary `the run went silent twice`. The worker never resumes a third time, preventing infinite resumption loops.

## Failure signature detector and tokens

A run ending silent without a result counts once toward the failure signature detector (JAIF-340).
Tokens used across all turns (`tokens_in` and `tokens_out`) are tracked and aggregated, and the total tokens used
are included in the maintenance Bug description and repeat comments in Jira.

## Dashboard Work tab metrics

`GET /v1/dashboard/silent-runs?days=30` returns developer run counts, silent run counts, `silent_share_pct`,
daily rows, ticket breakdown rows, and a `target_share_pct` of 2.0 percent for the caller's project scope.

The Command Center Dashboard Work tab at `/cc` displays a Silent runs panel tracking silent run share against
the 2.0% target, with daily trends and per-ticket breakdowns.

## Routes

- `GET /v1/dashboard/silent-runs` Developer silent-run counts are visible only for projects in the caller's Dashboard scope.
- `POST /v1/master/actions` Executes policy-checked master operational actions on tickets, accounts, and runs, or records and delivers owner escalations when human intervention is required.

**Console screens:** Command Center (`cc`)

