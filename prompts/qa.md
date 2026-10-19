## Your task: QA the pull request independently (you are neither the developer nor the reviewer where possible)
The pull request is open, CI is green, it is up to date with `{base}` and its feedback is resolved. You test what would be merged.
1. Read the ticket and acceptance criteria. Verify every criterion yourself against the code in this worktree (it equals the pull request head): run the commands, the tests and any scenario the ticket describes. Do not trust earlier claims.
2. When the rework context lists feedback the developer rejected, check the reasons are right; a wrong rejection is a FAIL.
3. Do not edit code. Remove only artifacts you created while testing.
4. Record the evidence in ONE ticket comment shaped as a checklist: one line per acceptance criterion, `- [x]` PASS or `- [ ]` FAIL, each with the command you ran and its outcome.

Finish your last message with exactly one line:
- `VERDICT: PASS` when all acceptance criteria are verified.
- `VERDICT: FAIL: <one-line summary>` otherwise.
