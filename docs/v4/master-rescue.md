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
| `total_work_minutes` | 120 | total work time of the ticket (In Progress and In Review; CI and the pull-request gate are not work time) |
| `master_minutes` | 60 | Claude's own rescue budget |
| `total_cap_minutes` | 180 | the ticket's cap: must cover `total_work_minutes` + `master_minutes` |
| `model` | empty | `Claude[:model[:effort]]` of the rescue, the strongest configured one; empty uses Claude's entry in `agent_models` |
| `taken_by_field` | `Taken by` | Jira field set to Claude |

Quota and Offline Blocked tickets are never handed to Claude (they return by themselves); `quota_wait_minutes` and
`offline_wait_minutes` are refused. `bin/aiflow-work check` validates the table (unknown names, negative waits, limits below 1,
a cap below the work limit plus the rescue budget, a model that is not Claude) and prints the values in use.

## What happens

1. **Qualifies.** The supervisor records work time per ticket (`worked:<KEY>`, added when In Progress or In Review is left). The
   operator turns a qualifying ticket into the incident `rescue:<KEY>`, instead of the generic `blocked` / `stuck` one.
2. **Owner at once**, without waiting: owner-only terms are checked in the summary, Blocked reason, description, and the last
   three comments, even when the ticket is only one minute into In Progress. This includes money, credentials, deleting data,
   a release, the requirements, merging by hand, runtime setup, or installed software. If Jira cannot provide the description
   or recent comments, the safety review is incomplete and the ticket goes to the owner. A ticket with no budget left under its
   cap or Claude unavailable (no machine has Claude: offline; Claude cooling down after a usage limit: quota) also goes to the
   owner. The ticket gets `needs-owner` and the usual question comment.
3. **Handover** otherwise: status `Handover` (JAIF-102), `Taken by = Claude`, the AI Model field set to the rescue model, the label
   `master-rescue`, and the lease of a running worker is released. The rescue record is stored as soon as Jira accepts Handover,
   so a failed label write is retried on later operator passes. The label and record `rescue:<KEY>` make it one rescue per ticket;
   a later problem follows the normal operator path.
4. **Audit comment** on the ticket: where it is stuck (status, time, and Blocked kind), what was tried (trigger, failed
   attempts or rework rounds, and whether recent comments were reviewed), the work time and the remaining budget
   (`min(master_minutes, total_cap_minutes - work time)`), and the model. Raw Blocked reasons and comment excerpts are omitted
   from audit text and logs so ticket content cannot copy credentials into stdout or OpenObserve.
5. **Unsuccessful rescue**: if the budget expires while rescue is active, or Jira moves the ticket out of the rescue workflow
   without a confirmed result, the flow doctor records the cause and a reference to recent findings (the original comments remain
   on the ticket; their raw excerpts are omitted), clears `Taken by`, and returns the ticket to
   development (`To Do`). An explicit successful result or a Jira-confirmed Done, Closed, or Resolved status closes the rescue
   successfully. Failed doctor comments or transitions stay pending and retry on later operator passes. An unsuccessful rescue is
   not escalated to a person and gets no `needs-owner`.

The Command Center shows the label and the status changes; the audit comments are the operator's usual ones. A failed Handover
transition does not add a rescue label or record, so the same incident becomes eligible again after its temporary operator
cooldown. Temporary `Taken by` and model fields are cleared; if Jira refuses a cleanup write, the operator retries it. When
Handover succeeds but a later Jira label update fails, the rescue record keeps the label write pending and the operator retries
it without delaying flow-doctor handling.

### Handover pickup dependency

This code records the `Handover` status, `Taken by = Claude`, AI Model field, and rescue budget; it does not start a Claude worker.
The legacy supervisor's worker stages do not include `Handover`. Rescue therefore depends on JAIF-102's Jira workflow/automation
to pick up a Handover ticket for Claude, use the AI Model field, and enforce the `master_minutes` budget while Claude is working.
The rescue timer here starts after Jira accepts Handover. If JAIF-102 only adds the status without that pickup behavior, rescue
execution is not active until that dependency is supplied.

What AI Flow checks after the merge: the `Handover` status and the `Taken by` field exist in the Jira projects (JAIF-102), and
the JAIF-102 pickup workflow routes a Claude handover using the model and budget recorded here.
