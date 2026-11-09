# Repairing a failed local gate

After the worker commits the developer's work, `run_local_gate` runs the configured plan. If one step has status
`failed`, the worker gives the same developer run one repair turn in the same worktree. The prompt names the failed
step and includes its redacted output. A missing plan, gate execution error, timeout, missing environment item, or
other incomplete-only result does not start a repair.

The existing named `docs-check` gap has a documentation-only repair scope: the missing `docs/v4` page and
`deploy/v4/README.md` topic index. Other failed steps may change the ticket work. A repair is accepted only when the
provider returns `DONE` without an error, the worktree HEAD is unchanged, and there are working-tree changes. The
worker commits the repair with its normal commit mechanism and runs the gate once more. There is no second repair in
that gate invocation.

If the repair is rejected or the repeated gate is not green, the worker uses the existing gate-failure report and
failed-work patch. It does not push. Only a final green gate continues to the existing pull request flow. Run details
retain the final gate step statuses and aggregate provider usage from both developer turns.

Gate step names come from the configured plan: the built-in plan uses the names in `aiflow/v4/core/local_gate.py`,
while project commands and checks use `check-1`, `check-2`, and so on. Add checks through the existing project gate
configuration or the built-in plan; the worker applies the same bounded repair rules to any failed step.
