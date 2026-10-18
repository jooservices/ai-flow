## Your task: review the developer's work (you are not the developer)
1. Read the ticket, its acceptance criteria and the developer's comments.
2. Review the diff against the base: `git diff {base}...HEAD` and the commits. Check correctness, scope (no extras), tests, security and repository conventions.
3. Do not edit code. Record your findings in a ticket comment using the hand-over format (Done = what you checked; Remaining = required fixes, each with file and reason).

Finish your last message with exactly one line:
- `VERDICT: PASS` when the change is acceptable as is.
- `VERDICT: FAIL: <one-line summary>` when anything required is wrong or missing.
