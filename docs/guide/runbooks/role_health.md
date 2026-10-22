# Runbook: role_health

**Assumes:** you are an Owner or Super Administrator who can open the Machines screen and inspect AI account runs. AI Flow v4 is installed.

## Symptom

The `role_health` warning says an AI account was switched off for one role and includes its failure score. That account can still work in its other roles, but AI Flow sends this role's work to another account.

## Cause

The account exceeded the configured failure score across its latest scored runs for this role. Failed or blocked work, timeouts and work sent back by verification count. Quota, sign-in, network, infrastructure and tool errors are excluded.

## Fix

1. Open **Machines** and find the account and role in **AI quality by role**. Read the note and check the account's recent runs and verification results for the failures.
2. Fix the cause shown by those runs, such as an account model or role configuration, a project tool problem, or repeated verification feedback.
3. The role receives one small probation ticket per UTC day. After two probation tickets succeed in a row, AI Flow switches the role back on and closes this incident. A failed probation ticket resets the streak.
4. If the threshold does not fit your operating policy, an Owner can change the run window or failure percentage in **AI quality by role**. Keep the threshold high enough to allow normal run variation.
