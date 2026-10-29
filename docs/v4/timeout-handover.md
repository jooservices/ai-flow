# Hand a timed-out developer run with work to verification

When a developer run hits the hard clock, AI Flow used to comment "no usable change", move the ticket to To Do,
and leave uncommitted work in the worktree. Pipeline checks never ran. After this change the worker keeps the
work.

On timeout `POST /v1/runs/{run_id}/result` records `outcome=timeout` and `error_kind=timeout`. If the worktree
has uncommitted files, AI Flow commits, pushes, and opens or reuses a draft pull request, then transitions to
In Verify. If the tree is clean but the branch already has commits or an open pull request, that pull request is
kept and the ticket still goes to In Verify. The run details mark the full suite as unfinished so pipeline
checks complete it.

Timeout with no dirty files, no branch commits and no pull request still returns to To Do. The next developer
claim uses the raised hard limit.

**Console screens:** none (worker handover; no UI in this ticket).
