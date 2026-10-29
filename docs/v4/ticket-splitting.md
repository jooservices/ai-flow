# Ticket split-plan contract

Intake and control share one function, `validate_plan` in `aiflow/v4/core/ticket_splitting.py`, so a later
ticket can create Jira children from a list that is already known to be usable. This page is the contract
only. It does not create Jira issues, estimate size, or change claim.

A valid plan is a list of **2 to 8** children. Each child is a dict with only `summary` and `description`
(strings). After strip, summary is 5 to 240 characters and description is 1 to 10,000 characters. Description
must contain a line that starts with `AC1:`, `- AC1:`, or `* AC1:` so a developer AI has one testable
criterion. `validate_plan` returns a **new** list of stripped dicts and does not mutate the input. Any other
shape raises `InvalidSplitPlan` with a message that names the failing index or the 2–8 bound.

**Console screens:** none (library contract; no UI in this ticket).
