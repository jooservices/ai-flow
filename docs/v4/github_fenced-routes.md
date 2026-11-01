# GitHub fenced routes

The fenced GitHub token, git proxy, pull request, merge, checks and review action routes. They live in `aiflow/v4/control/features/github_fenced/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-479 with their behaviour unchanged. Routes registered by `features/github_fenced/routes.py`:

- `GET /v1/github/checks`
- `GET /v1/github/git/{git_path:path}`
- `GET /v1/github/pr`
- `GET /v1/reviews/export.csv`
- `POST /v1/github/comment`
- `POST /v1/github/delete-branch`
- `POST /v1/github/git/{git_path:path}`
- `POST /v1/github/merge`
- `POST /v1/github/pr`
- `POST /v1/github/token`
- `POST /v1/github/update-branch`
- `POST /v1/github/webhook`
- `POST /v1/reviews/{key}/answer`
- `POST /v1/reviews/{key}/approve`
- `POST /v1/reviews/{key}/ask-ai`
- `POST /v1/reviews/{key}/reject`
- `POST /v1/reviews/{key}/rework`
- `POST /v1/reviews/{key}/skip-gate`

Routes that stay in `control/api.py` and are named here only because their handlers call code the docs check watches; they did not move:

- `GET /v1/github/ci-log`
- `GET /v1/master/autonomy`
- `GET /v1/master/context`
- `GET /v1/master/report`
- `POST /v1/master/actions`
- `POST /v1/master/flow-bugs`
- `POST /v1/master/plan-outcome`
- `POST /v1/runs/{run_id}/heartbeat`
- `POST /v1/runs/{run_id}/release`
- `POST /v1/runs/{run_id}/result`
- `POST /v1/work/claim`
- `PUT /v1/master/autonomy/budget`

## Audit

A successful fenced write publishes a domain event defined in `features/github_fenced/events.py`; its `write_audit` row
(target `github`, with `run_id` and `fence`) is written by the audit ledger listener in the same transaction. Events:
`github.token_issued` (`POST /v1/github/token`, direct), `github.git_relay_token_issued` (`POST /v1/github/token`,
control relay), `github.pr_lookup_completed` (`GET /v1/github/pr`), `github.pr_created` (`POST /v1/github/pr`, whose
`runs.pr_url` update is in the same transaction), `github.comment_posted` (`POST /v1/github/comment`) and
`github.branch_deleted` (`POST /v1/github/delete-branch`, only when GitHub accepted the call). A refused or failed write
keeps its `audit(..., accepted=False)` call. Git push, update-branch and pull request close now publish
`github.push_accepted`, `github.branch_updated` and `github.pr_closed` respectively.

Reviewer Jira and GitHub comments publish `review.jira_comment_posted` and `review.github_comment_posted`; ticket transitions,
label updates and accepted actions publish `review.ticket_transitioned`, `review.labels_updated` and `review.action_accepted`.
Successful reviewer merges publish `github.merge_completed`; a refused merge keeps its direct refused audit row.

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.
