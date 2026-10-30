# Release routes

The routes through which nodes read the tagged, signed release, report a failed release and sweep worktrees. They live in `aiflow/v4/control/features/release/routes.py` (request bodies in `bodies.py`), moved out of
`control/api.py` by JAIF-499 with their behaviour unchanged. Routes registered by `features/release/routes.py`:

- `GET /v1/releases/{sha}.tar.gz`
- `GET /v1/releases/{version}.tar.gz.sigstore.json`
- `GET /v1/releases/{version}/SHA256SUMS`
- `GET /v1/releases/{version}/manifest`
- `POST /v1/nodes/{name}/release-failures`
- `POST /v1/nodes/{name}/worktrees/sweep`

Routes that stay in `control/api.py` and are named here only because their handlers call code the docs check watches; they did not move:

- `GET /v1/version-releases/candidates`
- `POST /v1/environment/post-merge/complete`
- `POST /v1/version-releases/claim`
- `POST /v1/version-releases/{version_id}/complete`

See `docs/v4/event-architecture.md` for how `install(app, ctx, deps)` registers them.

Small helper extractions made in the same change, with no change to the routes that use them: the screenshot decoding of
`POST /v1/stakeholder/{key}/verdict` is `control/stakeholder_screenshot.py`, and the service checks of the System check
(`POST /v1/nodes/{name}/heartbeat`, `POST /v1/projects`, `POST /v1/system-check/run`) are `_service_conditions` in
`control/system_check.py`.
