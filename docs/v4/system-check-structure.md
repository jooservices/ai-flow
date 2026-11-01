# GitHub project condition structure

`_github_project_conditions` in `control/system_check.py` assembles GitHub readiness results in this order:

1. `_github_pending_conditions` supplies fixed pending results when provider checks are disabled.
2. `_github_selected_repositories` resolves each configured repository's connection and caches its accessible repositories.
3. `_github_installation_condition` reports the connection result, then `_github_permission_checks` and
   `_github_app_permissions_condition` report the selected GitHub App permissions.
4. `_github_repository_conditions` appends repository access and base-branch results for each configured repository.

Condition-specific functions build each displayed result. To add or change a result, keep its builder focused on one
condition, call it in the existing order, add a direct unit case for the builder, and add representative input and
literal output to `tests/v4/test_system_check_parity.py`.
