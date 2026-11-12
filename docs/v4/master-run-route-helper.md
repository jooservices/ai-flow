# Master run route helper

`control.route_deps.master_run(ctx, run_id, fence, node, action, payload=None)` lets a router apply the existing
master-run checks through its application context. Use `route_deps.context(request)` to get the request app's context;
the helper is not an authentication dependency, so production routes keep their node authentication.

The helper calls `fenced` first, then checks that the run step is `master`, then checks exact node ownership. It returns
the same run row and preserves the existing 404, 409 and 403 responses. `app_helpers.install` keeps its returned
`master_run` entry as a delegate for existing callers. New router code should call the context helper directly.
