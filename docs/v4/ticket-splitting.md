# Ticket splitting

## Split-plan contract

Intake and control share one function, `validate_plan` in `aiflow/v4/core/ticket_splitting.py`, so a later
ticket can create Jira children from a list that is already known to be usable. This page is the contract
only. It does not create Jira issues, estimate size, or change claim.

A valid plan is a list of **2 to 8** children. Each child is a dict with only `summary` and `description`
(strings). After strip, summary is 5 to 240 characters and description is 1 to 10,000 characters. Description
must contain a line that starts with `AC1:`, `- AC1:`, or `* AC1:` so a developer AI has one testable
criterion. `validate_plan` returns a **new** list of stripped dicts and does not mutate the input. Any other
shape raises `InvalidSplitPlan` with a message that names the failing index or the 2–8 bound.

## needs_split from the capacity estimate

Before intake writes children, `decide_split` in `aiflow/v4/control/ticket_split_decision.py` calls the existing
JAIF-247 `capacity.estimate` and `estimate_needs_split` in core. `needs_split` is true when
`estimate["time_minutes"]["high"]` is greater than the project target work minutes (default 30). The function
returns `{estimate, needs_split}`. It does not call Jira or mutate the ticket. A supplied plan is checked with
`validate_plan`; a missing plan is allowed at this step.

## Intake split list

When `needs_split` is true, intake returns an `INTAKE: SPLIT` JSON list that `validate_plan` accepts. A plan that
fails `validate_plan` is rejected and Jira is not written. A ticket that does not need splitting keeps the existing
`INTAKE: REWRITE` path. Intake still does not create child issues.

## Apply the plan to Jira

`apply_plan` in `aiflow/v4/control/ticket_split_apply.py` creates Story children from a valid plan. Each child
gets a stable `aiflow-split-<parent>-<index>` label so a second apply returns the same keys and does not duplicate
issues. Each child Relates to the parent; child N+1 is Blocked by child N. The parent loses `aiflow-ready` and
gains `aiflow-split-parent`. Children do not receive `aiflow-ready` here. `after_intake_result` runs from
`run_inbox.finish` after a successful intake `split_plan` on `POST /v1/runs/{run_id}/result`.

**Console screens:** none (library contract; no UI in this ticket).
