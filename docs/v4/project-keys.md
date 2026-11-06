# Project keys

## Maintenance tickets

`maintenance_project` is the single Jira project setting for flow bugs, canary rollback bugs, and maintenance
tickets. It defaults to `JAIF` and can be set with `AIFLOW4_MAINTENANCE_PROJECT`.

## Filing paths

Repeated flow failures go through `flow_bug_candidates` and `create_flow_bug`; canary rollbacks go through
`record_rollback` and `retry_pending_flow_bugs`. Other maintenance reports use `create_intervention_bugs`,
`open_environment_ticket`, `_create_pending_need_ticket`, `open_maintenance_ticket`, and scale-proof `file_tickets`.
Each filing path reads `ctx.settings.maintenance_project`.
Add another maintenance filing path by using that setting and testing the Jira project passed to `create_issue`.

## Review preview gate

`review_preview_epic` controls which project's preview links wait for a completed epic. It defaults to an empty
string, which disables previews, and can be set with `AIFLOW4_REVIEW_PREVIEW_EPIC`. The queue shows a preview
only when that epic is mirrored as `Done` and the project has a preview URL.

## Release profiles and examples

Version release profiles are opt-in in each project's configuration. There is no project-specific default profile.
Use the project's current Jira key in deployment examples and documentation. Tests may use isolated project keys.

The v4 pull-request test `test_product_code_defaults_and_docs_do_not_name_legacy_projects` scans product code,
defaults, deployment files, and docs for retired project keys. To add or update a project example, change it to
the current key and keep test-only keys in `tests/v4/`.
