# Master AI rescue

A ticket that stays Blocked too long, or runs over its work limits, is first handed to Claude as the master AI. A person
is involved only when Claude cannot or must not decide. This extends the operator of the running flow
(`aiflow/operator.py`, see [OPERATOR](../OPERATOR.md)); it reuses the operator incident queue (a `rescue:<KEY>` incident)
and its audit comments. It needs the operator to be enabled in `config/operator.toml`.

Rules live in `aiflow/master_rescue.py` (pure), the handover in `aiflow/rescue.py`.

## Settings: `[master]` in `config/aiflow.toml`

| Setting | Default | Meaning |
|---|---|---|
| `config_wait_minutes` | 10 | Blocked (Config) this long: rescue |
| `attempts_wait_minutes` | 0 | Blocked (Attempts) |
| `rework_wait_minutes` | 0 | Blocked (Rework) |
| `unclear_wait_minutes` | 30 | Blocked (Unclear) |
| `in_progress_minutes` | 60 | In Progress this long in one stretch: rescue |
| `total_work_minutes` | 120 | accumulated In Progress and In Review time, retained after later status moves; CI and the pull-request gate are not work time |
| `master_minutes` | 60 | Claude's own rescue budget |
| `total_cap_minutes` | 180 | the ticket's cap: must cover `total_work_minutes` + `master_minutes` |
| `model` | empty | `Claude[:model[:effort]]` of the rescue, the strongest configured one; empty uses Claude's entry in `agent_models` |
| `taken_by_field` | `Taken by` | Jira field set to Claude |

Quota and Offline Blocked tickets are never handed to Claude (they return by themselves); `quota_wait_minutes` and
`offline_wait_minutes` are refused. `bin/aiflow-work check` validates the table (unknown names, negative waits, limits below 1,
a cap below the work limit plus the rescue budget, a model that is not Claude) and prints the values in use.

## What happens

1. **Qualifies.** The supervisor records work time per ticket (`worked:<KEY>`, added when In Progress or In Review is left).
   Reaching `total_work_minutes` qualifies even after the ticket moves to QA or another status; the continuous 60-minute trigger
   applies only while In Progress. The operator turns a qualifying ticket into the incident `rescue:<KEY>`, instead of the generic
   `blocked` / `stuck` one.
2. **Owner at once**, without waiting: owner-only terms are checked in the summary, Blocked reason, description, and the last
   three comments, even when the ticket is only one minute into In Progress. This includes money, credentials, deleting data,
   a release, adding or changing requirements, merging by hand, runtime setup, or installed software. Ordinary phrasing such as
   “Add a new requirement,” “Remove production data,” and “Set up the runtime” is covered. If Jira cannot provide the description
   or recent comments, the safety review is incomplete and the ticket goes to the owner. A ticket with no budget left under its
   cap or Claude unavailable (no machine has Claude: offline; Claude cooling down after a usage limit: quota) also goes to the
   owner. The ticket gets `needs-owner` and the usual question comment.
3. **Handover** otherwise: status `Handover` (JAIF-102), `Taken by = Claude`, the AI Model field set to the rescue model, the label
   `master-rescue`, and the lease of a running worker is released. The rescue record is stored as soon as Jira accepts Handover,
   so failed label writes and the handover audit comment are retried on later operator passes. The saved audit marker prevents
   duplicate comments when Jira accepted a comment but its response was lost. The label and record `rescue:<KEY>` make it one
   rescue per ticket; a later problem follows the normal operator path.
4. **Audit comment** on the ticket: where it is stuck (status, time, and Blocked kind), what was tried (recorded failed
   developer/reviewer/QA/PR runs, rework rounds, and whether recent comments were reviewed), the work time and remaining budget
   (`min(master_minutes, total_cap_minutes - work time)`), and the model. Raw Blocked reasons and comment excerpts are omitted
   from audit text and logs so ticket content cannot copy credentials into stdout or OpenObserve.
5. **Unsuccessful rescue**: if the budget expires while rescue is active, or Jira moves the ticket out of the rescue workflow
   without a confirmed result, the flow doctor records the cause and a reference to recent findings (the original comments remain
   on the ticket; their raw excerpts are omitted). The worker also passes a bounded, redacted summary of Claude's actual run
   findings when it ends early, clears `Taken by`, and returns the ticket to
   development (`To Do`). An explicit successful result or a Jira-confirmed Done, Closed, or Resolved status closes the rescue
   successfully. Failed doctor comments or transitions stay pending and retry on later operator passes. An unsuccessful rescue is
   not escalated to a person and gets no `needs-owner`.

The Command Center shows the label and the status changes; the audit comments are the operator's usual ones. If a Handover
transition returns an error, the operator reads the Jira status before deciding: a confirmed Handover is recorded and completed,
a confirmed refusal clears temporary fields and permits a retry after a short cooldown, and an unreadable status remains pending for
reconciliation. Temporary `Taken by` and model fields are cleared after a confirmed refusal; if Jira refuses a cleanup write, the operator retries it. When
Handover succeeds but a later Jira label update fails, the rescue record keeps the label write pending and the operator retries
it without delaying flow-doctor handling. A failed handover audit comment stays pending and is retried; reconciliation checks for
its marker first so an accepted comment is not posted twice.

### Handover pickup

The existing worker observes `Handover` tickets, but starts work only when both the `master-rescue` label and an active rescue
record with a completed handover audit are present. It reserves a Claude session, resumes the normal developer worktree and
prompt, uses the model saved at handover, and stops the runner at the saved rescue deadline. A successful developer result
continues through the normal pull-request path. A run that times out, reports Blocked, or ends without committed work goes
through the flow doctor, which records the cause and returns the ticket to To Do with a safe reference to its findings. No
separate JAIF-102 worker automation is needed; JAIF-102 supplies the Handover status and `Taken by` Jira field.

The rescue timer starts just before the Handover request, so an ambiguous Jira response cannot extend the budget. Prompt, Jira
context, and environment preparation also consume the budget; the worker recalculates the remaining seconds immediately before
starting Claude and does not launch if setup exhausts it. After merge,
AI Flow verifies that the configured Jira projects expose the Handover status and `Taken by` field (JAIF-102).

## Acceptance criteria map

| Criterion | Implementation | Covering test |
|---|---|---|
| AC1 | `aiflow/master_rescue.py`: `WAIT_DEFAULTS`, `_parse_wait`, `trigger_of` | `tests/test_master_rescue.py`: `test_blocked_kinds_wait_their_own_time`, `test_quota_offline_and_other_kinds_are_never_handed_to_claude` |
| AC2 | `aiflow/master_rescue.py`: `trigger_of`; `aiflow/operator_rescue.py`: work-time snapshot | `tests/test_master_rescue.py`: `test_in_progress_stretch_and_total_work_limits`, `test_accumulated_work_still_qualifies_after_moving_into_qa` |
| AC3 | `aiflow/rescue.py`: `hand_over`; `aiflow/supervisor_flow.py`: Handover pickup | `tests/test_master_rescue.py`: `test_handover_sets_status_taken_by_label_model_and_audit`, `test_only_one_rescue_per_ticket`; `tests/test_supervisor_rescue.py`: `test_handover_stage_dispatches_only_an_active_record_to_a_reserved_claude_worker` |
| AC4 | `aiflow/rescue.py`: `lines`; `aiflow/master_rescue_worker.py`: saved model | `tests/test_master_rescue.py`: `test_handover_audit_uses_recorded_failed_runs_instead_of_the_attempt_limit`, `test_handover_sets_status_taken_by_label_model_and_audit` |
| AC5 | `aiflow/master_rescue.py`: budget defaults/cap; `aiflow/master_rescue_worker.py` and `aiflow/supervisor.py`: absolute deadline | `tests/test_master_rescue.py`: `test_budget_is_master_minutes_under_the_total_cap`; `tests/test_supervisor_rescue.py`: `test_executor_recalculates_budget_after_delayed_setup`, `test_executor_does_not_launch_after_setup_uses_the_remaining_budget` |
| AC6 | `aiflow/rescue.py`: `claude_unavailable`, `handle`, `to_owner` | `tests/test_master_rescue.py`: `test_unavailable_claude_goes_to_the_owner_at_once` |
| AC7 | `aiflow/master_rescue.py`: `owner_only_topic`, `evaluate` | `tests/test_master_rescue.py`: `test_owner_only_work_goes_to_the_owner_without_waiting`, `test_ordinary_owner_only_actions_are_detected_in_descriptions_and_comments` |
| AC8 | `aiflow/master_rescue_worker.py`: `finish_attempt`; `aiflow/rescue.py`: `safe_run_findings`, `flow_doctor`; `aiflow/operator.py`: cycle ordering and pending guard | `tests/test_supervisor_rescue.py`: `test_worker_run_findings_reach_the_flow_doctor`; `tests/test_master_rescue.py`: `test_cycle_recovers_rescues_before_syncing_or_running_ordinary_incidents`, `test_run_next_skips_a_stale_ordinary_incident_during_doctor_recovery` |
| AC9 | `aiflow/config.py`: master registry loading/check line | `tests/test_master_rescue.py`: `test_the_file_is_validated_by_the_configuration_load_and_the_check`, `test_invalid_settings_are_rejected` |
