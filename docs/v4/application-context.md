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
- Optional Jira, GitHub, credential and repository-refresh services used by existing handlers.

`Ctx` remains an alias for `ApplicationContext`, and the established `db` and `repo` attributes stay available so
existing handlers and callers continue to use the same object. New control modules should accept
`ApplicationContext` at their registration boundary and use its fields instead of closing over process globals.

The context is assembled by `aiflow/v4/cli.py:build_ctx` and passed to `aiflow/v4/control/api.py:create_app` and
`aiflow/v4/control/loops.py:run_loops`. It introduces no route or screen registration of its own; the route and
screen inventories are covered by the application-context snapshot test.
