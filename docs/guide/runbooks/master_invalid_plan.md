# Runbook: master_invalid_plan

**Assumes:** you are an Owner or Super Administrator with access to the Command Center, and AI Flow v4 is installed
with a master account configured. The control machine is available to an operator.

## Symptom

A critical `master_invalid_plan` incident appears. The master could not return a valid action plan for three cycles
in a row or five cycles in one hour. It retries once in each cycle with a strict JSON format reminder. A later
valid plan closes the incident automatically.

## Cause

The master model returned malformed JSON or a plan that does not match the required format, including after its
retry. This can happen when the model response is cut off, the selected model is unavailable, or the master prompt
and model output no longer agree on the plan format.

## Fix

1. Open [Command Center](../screens/cc.md) and check the recent master cycle and account health.
2. Ask an operator to review the control logs for the invalid response and confirm that the configured master
   account can reach its model provider.
3. Compare the response with `prompts/v4/master.md`. Correct provider or model problems through the normal account
   administration process; do not change the master prompt as an incident workaround.
4. Let the next master cycle run. A valid plan closes the incident automatically. If the incident stays open,
   share the incident detail and cycle time with the operator.
