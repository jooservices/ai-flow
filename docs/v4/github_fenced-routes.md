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

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.
