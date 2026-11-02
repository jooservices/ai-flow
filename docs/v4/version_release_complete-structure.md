# Version release completion structure

`version_release.complete` accepts a worker's checked report and publishes from that checked snapshot. Its ordered
steps are `_saved_release_result`, `_prior_check_failure_result`, `_resume_audited_merge`, `_live_gate_result`,
`_reported_check_failure`, `_validated_release_shas`, `_current_release_branch`, `_prepare_release_branch`,
`_merge_release_pull_request` (which calls `_open_release_pull_request` when needed), and `_record_release_merge`,
followed by `_publish_after_merge`.

To add a completion phase, give it a named module-level helper with explicit inputs, call it at the right point in
`complete`, and keep audit and event writes explicit in that helper. Extend the parity cases with the new branch and
its literal result and call sequence.

Routes: `POST /v1/version-releases/{version_id}/complete`.
