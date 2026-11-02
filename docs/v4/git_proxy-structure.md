# GitHub Git proxy structure

The Git smart HTTP relay (`github_git_proxy`) is one small handler made of named steps, so a refusal keeps its
original status, message and side effects.

**Routes:** `GET /v1/github/git/{git_path:path}` and `POST /v1/github/git/{git_path:path}`.

The handler keeps the credential check, the push acceptance event and the rejected-push audit in plain sight. The
steps around it are module-level functions in `aiflow/v4/control/features/github_fenced/routes.py`:

1. `_git_transfer_request` verifies the repository path against the credential and resolves the Git operation
   (`git-upload-pack` or `git-receive-pack`) from the method, path and query.
2. `_authorized_git_transfer` dispatches on the credential purpose; `_authorize_check_probe` keeps check probes
   read-only and limited to the project's configured repositories, while `_authorize_run_transfer` requires a live
   fenced run whose repository matches the credential (the branch is enforced at token issuance and by the handler's receive-pack target check; a push also needs a credential that carries a branch).
3. The handler reads the receive-pack targets, resolves the access-token connection, relays the request and then
   publishes `GitHubPushAccepted` or audits the rejected push.

To add a check or a purpose, add a step beside these helpers and call it explicitly from the handler; do not route
event publishing or auditing through a lookup table, because the documentation checker follows plain calls.
