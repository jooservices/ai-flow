# Worktree cleanup

Every ticket step checks out its own worktree under `/srv/aiflow/work/<project>/` on the node that runs it
(`<branch>` for dev, `<branch>--verify`, `<KEY>--intake`, `<KEY>--impact`, `<KEY>--triage-<run>`). Each holds a
full checkout with `node_modules` or `vendor`, so they must not pile up.

The node agent asks the control plane every ten minutes which directories can go
(`POST /v1/nodes/<name>/worktrees/sweep`, directories younger than ten minutes are never offered). The control
plane answers with:

- every directory of a ticket whose status is **Done**, and
- the per-run checkouts (intake, impact, triage) of any ticket, because each run creates a fresh one,

and never touches a ticket that has a run in progress. The node removes the directories (read-only triage
checkouts included), then runs `git worktree prune` in its mirrors. Tickets still in progress, in verify or in
rework keep their worktree, so rework continues on the same branch checkout.
