# Shared worker run steps

The developer, verification and impact workers use `aiflow/v4/worker/run_steps.py` for workspace paths, provider run
specs, heartbeat progress, run notes and common provider-result fields. It also owns the project environment resolver
used by developer and verification prompts. Impact keeps its existing empty run environment and target-time budget.

The main helpers are `worktree_path`, `project_env`, `project_environment`, `make_run_spec`, `note`,
`environment_notes`, `environment_status`, `RunProgress`, `set_run_progress` and `run_result_metadata`. Add shared
behavior there, and pass phase-specific values such as progress names or timeout budgets as arguments so each worker
keeps its current policy. `worker/notes.py` re-exports the note and environment helpers for existing callers.
