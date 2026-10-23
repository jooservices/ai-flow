# Runbook: backup_failure

**Assumes:** You are an Owner or Super Administrator on an installed AI Flow v4 control plane and can read the
Backup & Restore screen. An administrator can check the control service when storage or credentials need repair.

## Symptom

The Backup & Restore screen reports that the latest scheduled backup failed, and Command Center shows a warning
incident named **Command Center backup failed**.

## Cause

The control service could not create or store the encrypted backup. Common causes are an unavailable destination,
insufficient storage permissions, a missing encryption key, or invalid S3 connection settings or credentials. The
incident detail and screen show the safe error detail recorded by the backup job.

## Fix

1. Open [Backup & Restore](../screens/backup.md) and check the latest backup detail and destination settings.
2. Ask an administrator to make the destination available and writable. For S3, verify the endpoint, bucket, region
   and control service credentials without pasting credentials into the console.
3. Confirm the encryption key is available on the control machine. If it was downloaded before, retrieve the copy
   saved separately; the console cannot download it again.
4. Select **Run backup now**. A successful backup closes the incident. If it fails again, keep the error detail and
   ask the administrator to check the control service logs.
