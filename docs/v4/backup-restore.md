# Backup and restore

Command Center's **Backup & Restore** screen at `/backup` is available to owners and super administrators. It
configures the backup schedule, destination and retention; shows the latest backup and weekly restore-test result;
downloads the encryption key once; and builds the restore command for a selected backup.

## What a backup contains

Each encrypted backup includes:

- A PostgreSQL custom-format dump of the full control database.
- Stored files from the control, Jira, GitHub and console secret directories, the owner-managed
  `/etc/aiflow/secrets/runtime.env` file, and the project env directory on the control host. Project values managed
  from Settings keep a canonical copy there; replacement workers receive missing values encrypted on their first
  heartbeat before assigned workers start. The backup encryption key is excluded.
- The control repository's versioned policy files, without its `.git` directory or legacy runtime inventory
  (`accounts.yaml`, `nodes/` and project-level machine assignments). Machine profiles and assignments are in the
  database dump.
- A separate snapshot of managed knowledge, memory and project reports. Restore verifies the snapshot against the
  restored database.

The bundle manifest has SHA-256 checksums for each component. AES-256-GCM encrypts the entire bundle before it is
written to a destination. A random 256-bit key is created by the first **Download encryption key once** action.
The control machine keeps a private copy at `/etc/aiflow/secrets/control/backup.key` to run scheduled backups; the
key is never included in the stored-secrets component. The browser downloads a separate copy named
`aiflow4-backup-key`. Keep that copy away from the control machine. It cannot be downloaded again, and a backup
cannot be recovered if every copy of the key is lost.

The default schedule is daily and the default retention is 30 days. Backups older than the chosen number of days
are removed after a successful backup; backups within that window are kept. Owners can also queue a backup from
the screen.

## Destinations

Choose a local folder, a mounted path, or S3-compatible storage:

- **Local folder** defaults to `/srv/aiflow/backups` inside the control containers. Local folders must stay below
  that mounted path; the deployment maps it to the durable host folder `/srv/aiflow/control/backups`.
- **Mounted path** must be mounted into the control API containers, control-loops and control-restore at the same path.
  Add the host bind mount to the shared `x-app.volumes` section in the installed `deploy/v4/control/compose.yaml`
  before selecting that path in the screen.
- **S3-compatible storage** uses an HTTPS path-style S3 endpoint, bucket, optional prefix and region. The control process
  reads `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optional `AWS_SESSION_TOKEN` from its environment. For the
  standard deployment, put these variables in `/etc/aiflow/secrets/control/aiflow4.env` and recreate the app
  containers. Credentials are not saved in the backup configuration or displayed in Command Center. The client
  signs requests with AWS Signature Version 4 and supports listing, uploading, downloading and retention deletes.

The control image includes PostgreSQL 16's `pg_dump` and `pg_restore`, matching the control database server. The
Python dependency list is unchanged: encryption uses the already locked `cryptography` package from
`pyjwt[crypto]`, and S3 signing uses the existing HTTP client.

## Restore onto a new machine

Install the new control plane and make the selected backup reachable from its standard local path, mounted path or
S3 endpoint. Copy the downloaded `aiflow4-backup-key` to `~/Downloads/aiflow4-backup-key` on that machine. In
Command Center, select the backup and copy the displayed command. Run it on the new control host. It stops the
control API and loops, runs the restore in the matching control image with a writable mount for the control
repository, and recreates the services so they reload the restored secret files whether the restore succeeds or
fails.

The command accepts the selected backup and the key file; the S3 endpoint and region are included when needed.
S3 credentials must already be present in the new control environment. Restore decrypts and checks the bundle,
then reports an outcome for the database, stored secrets, control repository settings and knowledge. Keep the
command output for that report; it contains no secret values.

The target database is replaced by the backup. Restore the backup only onto a new control machine, as described by
the screen. Local or mounted restore paths must be reachable at the same path inside the control container.

## Weekly restore test and incidents

Control-loops tests the newest available backup every seven days. It creates a uniquely named temporary PostgreSQL
database and a temporary filesystem tree, restores all four components there, checks the knowledge snapshot, then
drops the database and temporary files. It never restores over the live database, secrets or control repository.
The screen shows the most recent test and a result for every component. A failing test opens the existing warning
incident `backup_restore_test`; a later passing test closes it.

The tests in `tests/v4/test_backups.py` cover owner access and configuration, the one-time key, a full sample
installation round trip, wrong and missing keys, retention and the failing-test incident. The database dump
commands are isolated behind the same command runner used by production so the round-trip test can run in the
project's shared PostgreSQL test service without requiring PostgreSQL client binaries on the test host.
