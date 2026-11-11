# Runbook: verify_no_verdict

**Assumes:** you can open the ticket and the Command Center incidents list.

## Symptom

The flow doctor raises one `verify_no_verdict` warning for a ticket in verification. Its title names the ticket and
reports that every eligible account has at least one completed verification without a verdict in the current
development round.

## Cause

Each eligible account has at least one current-round verification without the required `VERDICT:` line. This warning
does not mean every account is barred: the separate offer policy still bars an account only after two consecutive
no-verdict results. The usual causes are a provider that answers in the wrong format or a ticket whose check output is
too large for the verifier.

## Fix

Open the verify runs of the ticket and read why each eligible account missed a verdict. Fix the cause (for example the
verify notes or the machine), or verify the ticket by hand. The incident stays open during a temporary capacity loss;
it closes when the ticket leaves verification or an eligible account has no missing-verdict result in the current
round. A successful new development run starts a fresh history round. See
[Verification exhaustion incident](../verify-no-verdict-incident.md).
