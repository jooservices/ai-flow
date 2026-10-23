# Backup & Restore

**Assumes:** You are signed in to an installed AI Flow v4 control plane as an Administrator. You
need `backup.read` to view this screen and `backup.manage` to change settings, run a backup or download its key.

Open **Backup & Restore** from the console menu. The page shows the latest backup, its destination and the result of
the weekly restore test.

## Set up backups

Choose a schedule, destination and retention period, then select **Save settings**. Local backups use the configured
backup folder. A mounted destination must already be mounted at the same path in the control API and control-loops
containers. For S3 storage, an administrator must configure the control service's AWS credentials before you choose
the endpoint, bucket, folder prefix and region. Credentials are not saved in the screen settings.

Select **Run backup now** to queue an extra backup. The page refreshes to show its status and the latest restore-test
result. A failed restore test opens an incident; follow its [runbook](../runbooks/backup_restore_test.md).

## Save the encryption key

Select **Download encryption key once** and keep the downloaded key somewhere separate from the control machine.
The screen cannot download it again. Without this key, encrypted backups cannot be restored. The control service
keeps its own private copy so scheduled backups can run.

## Restore a backup

Restore only onto a new control machine. Make the selected backup available there and copy the saved key to that
machine. Select the backup, copy the restore command shown on the page and run it on the new control host. The
command stops the control services during restore and starts them again afterward. Review its outcome before using
the restored installation.

If no backup is listed, wait for the first scheduled backup or check that the configured destination is available.
If the key was already downloaded, retrieve the copy you saved separately; the console cannot issue another one.
