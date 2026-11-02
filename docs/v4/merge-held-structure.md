# Merge-held decision structure

`_merge_held` in `control/merge_flow.py` keeps the lease while it checks GitHub and either records a hold reason or
finishes the merge. `_finish_if_github_settled` handles a pull request already merged or closed. The queue preflight
checks run in this order: `_check_queue_updated`, `_check_queue_conflict`, `_check_queue_busy`, and
`_check_branch_update_failed`. The merge response checks keep their own order: busy, updated, conflict, then update
failure. `_check_pr_conflict`, `_check_failed_checks`, `_check_pending_checks`, `_check_head_changed`, and
`_record_github_refusal` handle the later refusal reasons. `_complete_successful_merge` records a successful merge.

To add a hold reason, add one named check for its message and side effects, then place its call at the same position
in the relevant ordered handler. Keep the literal parity outputs in `tests/v4/test_merge_held_parity.py` aligned with
the current behavior and add a direct unit test for the check.

Routes: `POST /v1/github/webhook` can trigger the merge step when GitHub webhooks are configured.
