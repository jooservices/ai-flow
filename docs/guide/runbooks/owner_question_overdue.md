# Runbook: owner_question_overdue

**Assumes:** you are an Owner or Super Administrator and can answer decisions in Command Center. Raised in `control/escalation_deadline.py`.

## Symptom

Warning incident "owner question D<n> passed its 12-hour deadline". A ticket waits for an answer that has not come.

## Cause

The question could not be applied automatically after its deadline: it is about an irreversible or production action, or it has no recommended option. Every other overdue question is decided with its recommendation and does not raise this incident.

## Fix

1. Open the question in the decision register of Command Center and read the options.
2. Answer it with one of the options (or the recommended one). The waiting tickets resume at once.
3. If the question should never need you, add the rule to the register with a pattern, so the next similar question is answered without a person.
4. The incident closes when the question is answered.
