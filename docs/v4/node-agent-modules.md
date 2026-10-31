# Node agent modules

`aiflow.v4.node.agent` keeps the node loop, heartbeat, reconciliation, and compatibility exports for existing callers.
Its cohesive responsibilities live beside it:

- `operator_audit.py` parses, redacts, and forwards operator audit events.
- `provider_sign_in.py` prepares account homes and manages provider sign-in subprocesses.
- `cleanup.py` removes finished worktrees, old scratch files, and stale test databases, and reports disk cleanup.
  The privileged removal of e2e artifacts is pinned (JAIF-515): the sweep records each worktree's device and inode when it scans,
  the cleanup opens the directory, refuses when the identity differs, and the root container mounts the open descriptor
  (`/proc/<pid>/fd/<n>`) instead of the path, so the root container never touches a worktree replaced after the scan.
  The host-side removal after the identity check still uses paths and is not covered by this guarantee.
- `release.py` verifies release signatures and schedules release or develop self-updates.

The extracted modules do not import `agent`. `agent` re-exports only the names its own loop calls or that other modules and tests
still reach through it (for example `clean_scratch`, `installed_release`, `self_update`, `run_provider_sign_in`); every other moved
function is imported from the module that owns it. Module state that is rebound with `global` (the cleanup timestamps) is never
re-exported, because a copy would go stale.
