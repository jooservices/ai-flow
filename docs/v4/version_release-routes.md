# Version release routes

The configured Jira version-release routes (candidates, claim, complete, post-merge). They live in `aiflow/v4/control/features/version_release/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-499 with their behaviour unchanged. Routes registered by `features/version_release/routes.py`:

- `GET /v1/version-releases/candidates`
- `POST /v1/environment/post-merge/complete`
- `POST /v1/version-releases/claim`
- `POST /v1/version-releases/{version_id}/complete`

Routes that stay in `control/api.py` and are named here only because their handlers call code the docs check watches; they did not move:

- `GET /v1/releases/{sha}.tar.gz`
- `GET /v1/releases/{version}.tar.gz.sigstore.json`
- `GET /v1/releases/{version}/SHA256SUMS`
- `GET /v1/releases/{version}/manifest`
- `POST /v1/nodes/{name}/release-failures`
- `POST /v1/nodes/{name}/worktrees/sweep`

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.
