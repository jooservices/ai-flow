# Runbook: backup_restore_test

**Assumes:** You are an Owner or Super Administrator on an installed AI Flow v4 control plane and can read the
Backup & Restore screen. The control service runs a weekly restore test in temporary storage.

## Symptom

The weekly restore test shows **failed** on the Backup & Restore screen, and Command Center shows a warning incident
named **Weekly backup restore test failed**.

## Cause

The control service could not list, decrypt or restore the newest backup in its isolated test database and temporary
files. The screen shows which component failed. The test never restores over the live database or stored files.

## Fix

1. Open [Backup & Restore](../screens/backup.md) and read the latest restore-test result and component details.
2. If the destination cannot be read, restore access to it. If decryption failed, ask an administrator to check that
   the control machine's private key matches the separately saved key.
3. If a component restore failed, check the control service logs and available database and disk space. Do not test
   by restoring over the live installation.
4. The service tests the newest backup again on its next weekly check. A passing test closes the incident. If the
   next test also fails, keep its component details and ask the administrator to investigate the backup or storage.
