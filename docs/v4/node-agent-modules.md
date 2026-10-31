# Node agent modules

`aiflow.v4.node.agent` keeps the node loop, heartbeat, reconciliation, and compatibility exports for existing callers.
Its cohesive responsibilities live beside it:

- `operator_audit.py` parses, redacts, and forwards operator audit events.
- `provider_sign_in.py` prepares account homes and manages provider sign-in subprocesses.
- `cleanup.py` removes finished worktrees, old scratch files, and stale test databases, and reports disk cleanup.
- `release.py` verifies release signatures and schedules release or develop self-updates.

The extracted modules do not import `agent`; callers can keep importing the moved functions from `aiflow.v4.node.agent`.
