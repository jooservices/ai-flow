# Run repository selection

Run repository selection is shared by routers through `aiflow.v4.control.app_helpers.repository_for_run(ctx, run, project=None)`.
An external router passes `request.app.state.ctx`; it does not need the helpers installed by `create_app`.

The selector first returns `run.details.repository` when present. Otherwise it uses the supplied project or looks up
`run.project` in the control repository. With a project, it reads the ticket type and parent, resolves its epic through
`aiflow.v4.control.ticket_helpers.ticket_epic(ctx, ticket)`, and passes that ticket to `Project.repo_for`. A missing
project still returns an empty string. The callable returned by `app_helpers.install` keeps its original
`repository_for_run(run, project=None)` signature and delegates to the context-based helper.

The projects feature keeps `_ticket_epic(runtime, ticket)` as its existing runtime wrapper. It delegates to the shared
context helper, which follows parent tickets through the ticket repository until it finds an epic, a missing ancestor,
or a repeated key. Extend the shared helper and its characterization tests when this resolution rule changes; keep
routers calling the context-based helper directly.
