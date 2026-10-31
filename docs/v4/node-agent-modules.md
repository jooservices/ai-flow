# Node agent modules

`aiflow.v4.node.agent` keeps the node loop, heartbeat, reconciliation, and compatibility exports for existing callers.
Its cohesive responsibilities live beside it:

- `operator_audit.py` parses, redacts, and forwards operator audit events.
- `provider_sign_in.py` prepares account homes and manages provider sign-in subprocesses.
- `cleanup.py` removes finished worktrees, old scratch files, and stale test databases, and reports disk cleanup.
- `release.py` verifies release signatures and schedules release or develop self-updates.

The extracted modules do not import `agent`. `agent` re-exports only the names its own loop calls or that other modules and tests
still reach through it (for example `clean_scratch`, `installed_release`, `self_update`, `run_provider_sign_in`); every other moved
function is imported from the module that owns it. Module state that is rebound with `global` (the cleanup timestamps) is never
re-exported, because a copy would go stale.
