# Codex sandbox and Git commits

## Decision

Codex developer runs use `workspace-write`. Codex may edit the ticket worktree and the configured shared
scratch/cache root, but it does not receive `danger-full-access` or writable access to Git metadata. The worker
commits and pushes after Codex exits, then opens the draft PR and moves the ticket to In Verify. The adapter
downgrades an explicit `danger-full-access` request to `workspace-write` as a final guard.

This follows the existing v4 run boundary: the adapter runs the AI process, while `aiflow/v4/worker/dev.py`
checks the changed files and invokes `Git.commit_all`, `Git.push`, and the PR hand-off afterward. Codex does not
need to write `.git` to complete a developer run.

## Evaluated options

### A. Codex edits; the worker commits

Adopted. The default `workspace-write` sandbox makes the current worktree writable and leaves the Git metadata
protected. `sandbox_workspace_write.writable_roots` contains only `scratch_root(spec)` for the shared caches and
temporary files. The worker can then commit the edited worktree outside Codex's sandbox. This preserves the
existing dev flow and avoids adding a second process with access to repository internals.

### B. Add Git metadata to Codex's writable roots

Rejected. A linked worktree's `.git` file points to a per-worktree directory inside the repository's common Git
directory. The per-worktree directory holds state such as its index and `HEAD`, but a commit also writes objects
and updates branch references and logs in the common directory. In AI Flow that common directory is the shared
bare mirror, used by all ticket worktrees for the repository. Making it writable would expose objects, refs,
configuration, and worktree registration for other tickets to mutation by the AI. A root limited to the
worktree-specific metadata directory would not be sufficient to create commits; granting the common directory
would be much broader than one ticket's worktree.

## Verification and boundaries

The adapter test checks that even a `RunSpec` requesting `danger-full-access` produces `--sandbox workspace-write`
and adds only the shared scratch/cache root. It also sets `sandbox_workspace_write.exclude_slash_tmp=true`, since
Codex otherwise includes `/tmp` among the sandbox's writable locations. Codex's `workspace-write` mode supplies
write access to `--cd`'s worktree; Git metadata is not added as a writable root. A linked-worktree test exercises
the worker's post-run commit path with `Git.commit_all`.

This decision accepts write access to the worktree and the shared scratch/cache root, as required for edits and
package caches. Writes elsewhere, including the repository mirror's Git metadata, remain outside the configured
writable roots and are blocked by Codex's sandbox. The worker's later Git commit is a separate process outside
the AI sandbox.
