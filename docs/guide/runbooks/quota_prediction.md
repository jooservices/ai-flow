# Runbook: quota_prediction

**Assumes:** you are an Owner or Super Administrator who can open [SA health](../screens/sa.md), and AI Flow v4 is
installed. The incident detail includes the account and forecast information.

## Symptom

The `quota_prediction` warning says an AI account may run out of quota before its provider resets the usage window.
It names the predicted exhaustion time and the affected windows.

## Cause

Recent run usage or provider quota samples show that the account is likely to use its remaining quota before the
window resets. The forecast is based on recorded usage and includes a confidence level.

## Fix

1. Check the account's quota windows and reset times on the SA health screen.
2. If other accounts for that role are ready, let AI Flow route new work to them first while this account recovers.
3. If the role has too little capacity, add or enable another suitable account in SA health.
4. The warning closes automatically when the forecast no longer predicts exhaustion before reset.
