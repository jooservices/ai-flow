# Run repository helpers

The GitHub fenced routes use two shared helpers for run-specific repository settings and repository matching.
`aiflow.v4.control.run_repository_helpers.settings_for_run(ctx, run, project)` returns the saved
`details.repository_settings` dict when it has a truthy `repo`. Otherwise it asks the project for settings using the
existing `app_helpers.repository_for_run(ctx, run, project)` resolver. That resolver prefers the repository saved on
the run and retains the legacy ticket and epic fallback for older runs.

`matches_run_repository(repository, requested)` trims and lowercases the requested name, then accepts either the full
repository path or the name after its first slash, case-insensitively. The installed app helper functions remain
compatibility wrappers with their existing signatures; `repository_for_run(run, project=None)` remains available from
`app_helpers`.

This extraction does not register or move routes. The app factory continues to pass the same helper callables to the
GitHub fenced routes.
