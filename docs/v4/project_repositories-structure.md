# Project repository save structure

## Routes

- `PUT /v1/projects/{key}/repositories` saves the repository list and Jira epic mappings for a project. The route
  keeps authorization, refusal audits, the commit error audit, connection assignment events, the saved event and the
  changed event in their original order.

## Steps

- `load_project_repository_save_context` reads the current Jira epics, loads the GitHub repository catalog and maps
  each connected GitHub connection ID to its gateway.
- `collect_requested_project_repositories` trims requested repository names, checks visibility and selected
  connections, rejects duplicates, validates base branch syntax and stack names, and builds normalized entries.
- `project_epic_repository_mappings` trims epic keys and targets and omits mappings with blank targets.
- `add_epic_repository_mappings` checks that each mapping names a Jira epic and visible repository, then adds
  mapped repositories that are not already in the request.
- `include_default_project_repository` adds the project's primary repository if no request or epic mapping included it.
- `first_project_repository_branch_error` resolves each repository's chosen or catalog connection and checks its
  configured base branch through that connection's gateway.
- `validate_project_repository_save` calls those selection steps in refusal order and returns the repository set,
  chosen connections, trimmed mappings and first refusal.
- `commit_project_repository_settings` sorts the repository entries, writes them and the epic mappings into the
  project's control-repository YAML, then commits that file through the control GitHub gateway.

To extend this flow, add a named helper for a data preparation or validation phase and call it from the route at the
point where that phase belongs. Keep audits and event publication as explicit route calls so their order remains
visible.
