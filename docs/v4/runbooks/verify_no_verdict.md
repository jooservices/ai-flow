# Runbook: verify_no_verdict

**Assumes:** you can open the ticket and the Command Center incidents list.

## Symptom

The flow doctor raises one `verify_no_verdict` warning for a ticket in verification. Its title names the ticket and
says that every eligible account ended without a verdict. The ticket also appears under "Waiting for verification over
30 minutes" on the Delivery Dashboard, with the accounts that tried.

## Cause

Each eligible account ended the verification of the ticket twice in a row without the required `VERDICT:` line, so none
of them is offered the ticket again in this development round. The usual causes are a provider that answers in the wrong
format or a ticket whose check output is too large for the verifier.

## Fix

Open the latest verify runs of the ticket and read why the answer had no verdict. Fix the cause (for example the
verify notes or the machine), or verify the ticket by hand. A new development run starts the count again and closes the
incident. See [Epics and verification without a verdict](../verify-no-verdict.md).
