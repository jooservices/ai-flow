# Typed control application context

The control API creates one `ApplicationContext` and shares that object with its feature modules, the app state and
the background loops. Modules can use the same typed dependencies without importing the API entry point.

The context provides:

- `settings`: validated control settings.
- `database` (`db` compatibility alias): the control database.
- `repositories` (`repo` compatibility alias): a callable that returns the current control-repository snapshot.
- `repo_configuration`: the configured control-repository path from settings.
- `leases` and `events`: the shared fencing and event services.
- `clock`: a UTC clock implementing `now()`, replaceable when a feature needs deterministic time.
- `configured_repo`, `worker_resource_counts`, `system_check_integration_cache`, `event_registry`, `github_registry` and
  `github_connection_for_repository`: services that `create_app` and the loops attach after construction. They are declared fields
  with defaults, so no module reads them with `getattr` or sets a private `_name` on the context (JAIF-465). The context
  itself is the typed container; a second container class would only add a layer.
- Optional Jira, GitHub, credential and repository-refresh services used by existing handlers.

`Ctx` remains an alias for `ApplicationContext`, and the established `db` and `repo` attributes stay available so
existing handlers and callers continue to use the same object. New control modules should accept
`ApplicationContext` at their registration boundary and use its fields instead of closing over process globals.

The context is assembled by `aiflow/v4/cli.py:build_ctx` and passed to `aiflow/v4/control/api.py:create_app` and
`aiflow/v4/control/loops.py:run_loops`. It introduces no route or screen registration of its own; the route and
screen inventories are covered by the application-context snapshot test.

## Routes that read these services

No route changes behaviour. The routes that reach the declared fields are the claim and candidate routes
(`GET /v1/work/candidates`, `POST /v1/work/claim`, `GET /v1/ba-chat/candidate`, `GET /v1/master/candidate`,
`GET /v1/version-releases/candidates`, `POST /v1/version-releases/claim`), the ticket moves
(`POST /v1/jira/{key}/transition` and the review actions `POST /v1/reviews/{key}/answer`, `POST /v1/reviews/{key}/approve`,
`POST /v1/reviews/{key}/ask-ai`, `POST /v1/reviews/{key}/reject`, `POST /v1/reviews/{key}/rework` and
`POST /v1/reviews/{key}/skip-gate`, which publish through `event_registry`), and the readiness checks (`POST /v1/nodes/{name}/heartbeat`,
`POST /v1/projects`, `POST /v1/system-check/run`, which use `system_check_integration_cache`).
