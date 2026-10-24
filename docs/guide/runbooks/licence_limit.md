# Runbook: licence_limit

**Assumes:** you are an Owner or Super Administrator and AI Flow v4 is running. Raised in `control/licensing.py`.

## Symptom

Critical incident "Licence limit stops new work: machines|ai accounts|projects N/M". Workers are offered no ticket and
the reason names the limit, for example "Machines limit of 1 reached (3/1)". The alert goes to the owner's channels.

## Cause

Usage is above a plan limit and the 14-day grace period for usage above a newly applied or lowered limit has ended.
Running work is not cut off; only new work for the machines, AI accounts or projects beyond the limit is refused.
An installation with no licence configured has no limits and never raises this incident.

## Fix

1. Open the **Licence** screen and compare the plan limits with the usage.
2. Raise the limit for the plan, or remove or disable the extra machines, AI accounts or projects.
3. Check that the idle-flow alert ([flow_idle](flow_idle.md)) names the same limit as its cause.

The incident closes by itself when work resumes.
