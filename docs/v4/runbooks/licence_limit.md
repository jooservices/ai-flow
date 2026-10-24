# Runbook: licence_limit

**Assumes:** you are an Owner or Super Administrator with access to the Licence screen.

## Symptom

The control plane raises one critical `licence_limit` incident and alerts the owner's channels when a licence limit
stops new work: ready tickets get no worker, and the idle-flow alert names the licence limit as the cause. The incident
names the limit (machines, AI accounts, projects or users), the current usage and the action.

## Cause

The installation has a licence service or issuer key configured, its plan limit is lower than the current usage, and
the 14-day grace period for that excess has ended. Installations with no licence service or issuer key never have
limits.

## Fix

Open the Licence screen and either raise the plan limit (limits are editable) or activate a licence with a higher
plan, or remove the extra machines, AI accounts or projects. The incident closes by itself when work resumes. See
[Licensing and usage](../licensing.md).
