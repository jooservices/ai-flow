# Runbook: quota_high

**Assumes:** you are an Owner or Super Administrator and the AI provider reports usage windows (Claude: 5-hour and weekly). Raised in `control/loops.py`.

## Symptom

Warning incident "ACCOUNT 5-hour limit at 90%" (or weekly); critical at 100%.

## Cause

The account used 90% or more of a provider window. At 100% it pauses until the window resets and other accounts take its tickets.

## Fix

1. Read the reset time in the detail.
2. If other accounts of the role are ready, do nothing: the flow continues.
3. If capacity is short, add an account: [SA health](../screens/sa.md) recommends it when all accounts of a role stay in cooldown.
4. The incident closes when usage falls below 85%.
