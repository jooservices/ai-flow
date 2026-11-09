# Node installer rollback and backup recovery

The node agent installs the pinned server-installer at
`/opt/server-installer/v1.1.0/bin/server-installer`. It stages a replacement beside that directory so the old
installer can be restored if swapping in the new version fails.

## Replacement steps

1. `_ensure_server_installer` downloads and extracts the pinned source into temporary staging.
2. `_replace_server_installer` moves the existing version into staging and attempts to install the candidate.
3. `_restore_previous_installer` restores the old version if the candidate swap fails.
4. If restoration also fails, `_preserve_installer_backup` keeps the old files at
   `/opt/server-installer/v1.1.0.previous` when that path is available. If it already exists or the move there
   fails, `_keep_installer_backup` uses a unique sibling path such as
   `/opt/server-installer/v1.1.0.previous-<suffix>`. If moving to that unique path also fails, it copies the
   backup under that reserved directory. Existing backups are never replaced. If the fallback directory cannot
   be created or the copy fails, provisioning retains the temporary staging directory with the complete original
   backup at its `previous` path and reports that actual path plus the replacement, rollback, and preservation
   failure types.

## Operator recovery

The Command Center node operation log reports the actual preserved path and the replacement and rollback error
types. When fallback preservation itself fails, that path is inside the retained staging directory, for example
`/opt/server-installer/.v1.1.0-<suffix>/previous`. Use the reported path to locate the complete previous installer
directory. With node filesystem access, move any incomplete `/opt/server-installer/v1.1.0` directory aside, then
restore the preserved directory to that path. Keep any other `.previous` or uniquely named backup in place until
recovery is confirmed. After restoring the installer, use the existing node repair action in Command Center if
the requested node state still needs repair.

Changes to this flow belong in the named steps above and the deterministic rollback tests in
`tests/v4/test_server_installer_rollback.py`; keep the backup paths and operator guidance in sync.
