from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from aiflow.v4.node import agent, operations

# Numbers of the os.replace calls in _replace_server_installer, in order: source to staged, install to backup,
# staged to install, backup to install (the restore), backup to the preferred kept path, then a unique kept path.
REPLACE_STAGED_TO_INSTALL = 3
REPLACE_BACKUP_TO_INSTALL = 4
REPLACE_BACKUP_TO_PREFERRED = 5
REPLACE_BACKUP_TO_UNIQUE = 6


def _prepare_old_and_new_installers(tmp_path: Path) -> tuple[Path, Path, Path]:
    install_dir = tmp_path / "opt" / "server-installer" / "v1.1.0"
    (install_dir / "bin").mkdir(parents=True)
    (install_dir / "bin" / "server-installer").write_text("old", encoding="utf-8")
    source_dir = tmp_path / "extract" / "server-installer-v1.1.0"
    (source_dir / "bin").mkdir(parents=True)
    (source_dir / "bin" / "server-installer").write_text("new", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    return install_dir, source_dir, staging


def _patch_replace(monkeypatch: pytest.MonkeyPatch, failures: dict[int, OSError]) -> None:
    """Make the numbered os.replace calls raise the given errors and let every other call through."""
    real_replace = os.replace
    calls: list[int] = []

    def fake_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        calls.append(len(calls) + 1)
        if calls[-1] in failures:
            raise failures[calls[-1]]
        real_replace(source, destination)

    monkeypatch.setattr(operations.os, "replace", fake_replace)


def _prepare_ensure_installers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    install_dir, _source_dir, _staging = _prepare_old_and_new_installers(tmp_path)
    binary = install_dir / "bin" / "server-installer"
    binary.unlink()
    (install_dir / "lib").mkdir()
    (install_dir / "lib" / "previous-module.sh").write_text("old installer files", encoding="utf-8")
    monkeypatch.setattr(operations, "SERVER_INSTALLER", binary)

    def source_installer(extract_dir: Path) -> Path:
        source_dir = extract_dir / "server-installer-v1.1.0"
        (source_dir / "bin").mkdir(parents=True)
        (source_dir / "bin" / "server-installer").write_text("new", encoding="utf-8")
        return source_dir

    monkeypatch.setattr(operations, "_server_installer_source", source_installer)
    return install_dir, install_dir.parent


def _reported_operation_error(monkeypatch: pytest.MonkeyPatch, error: operations.OperationError) -> str:
    calls = []

    class FakeApi:
        def call(self, method, path, body=None, **kwargs):
            calls.append((method, path, body, kwargs))
            return {"operation": {"id": "op-backup", "payload": {"action": "repair"}}} \
                if method == "GET" else {"status": body["status"]}

    def fail_operation(_operation):
        raise error

    monkeypatch.setattr(operations, "execute", fail_operation)
    agent.process_node_operation(FakeApi(), "node-test")

    result = calls[-1][2]["result"]
    assert calls[-1][1] == "/v1/nodes/node-test/operations/op-backup/result"
    assert calls[-1][2]["status"] == "failed"
    return result["error"]


def _assert_operation_error_reports_backup(monkeypatch: pytest.MonkeyPatch,
                                           error: operations._InstallerBackupPreservationError,
                                           *details: str) -> None:
    reported_error = _reported_operation_error(monkeypatch, error)
    assert len(reported_error) <= 300
    assert str(error.backup_path) in reported_error
    assert "replacement=OSError" in reported_error
    assert "rollback=PermissionError" in reported_error
    assert all(detail in reported_error for detail in details)


def test_restore_failure_keeps_the_backup_and_reports_both_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_dir, source_dir, staging = _prepare_old_and_new_installers(tmp_path)
    _patch_replace(monkeypatch, {
        REPLACE_STAGED_TO_INSTALL: OSError("device busy"),
        REPLACE_BACKUP_TO_INSTALL: PermissionError("denied"),
    })
    kept_dir = install_dir.with_name("v1.1.0.previous")

    with pytest.raises(operations.OperationError) as error:
        operations._replace_server_installer(install_dir, source_dir, staging)

    message = str(error.value)
    assert "replacement=OSError" in message
    assert "rollback=PermissionError" in message
    assert str(kept_dir) in message
    assert (kept_dir / "bin" / "server-installer").read_text(encoding="utf-8") == "old"


def test_ensure_preserves_backup_when_preferred_move_fails_and_cleans_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    install_dir, installer_root = _prepare_ensure_installers(tmp_path, monkeypatch)
    replacement_error = OSError("private replacement detail")
    rollback_error = PermissionError("private rollback detail")
    _patch_replace(monkeypatch, {
        REPLACE_STAGED_TO_INSTALL: replacement_error,
        REPLACE_BACKUP_TO_INSTALL: rollback_error,
        REPLACE_BACKUP_TO_PREFERRED: OSError("private preservation detail"),
    })

    with pytest.raises(operations.OperationError) as error:
        operations._ensure_server_installer()

    message = str(error.value)
    assert "replacement=OSError" in message
    assert "rollback=PermissionError" in message
    assert "private" not in message
    backup_path = error.value.backup_path
    assert backup_path.parent == installer_root
    assert backup_path.name.startswith("v1.1.0.previous-")
    assert (backup_path / "lib" / "previous-module.sh").read_text(encoding="utf-8") == "old installer files"
    assert not install_dir.with_name("v1.1.0.previous").exists()
    assert not [path for path in installer_root.iterdir() if path.name.startswith(".v1.1.0-")]
    assert error.value.__cause__ is rollback_error
    assert rollback_error.__context__ is replacement_error


def test_ensure_preserves_backup_without_overwriting_existing_previous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    install_dir, installer_root = _prepare_ensure_installers(tmp_path, monkeypatch)
    existing_backup = install_dir.with_name("v1.1.0.previous")
    (existing_backup / "bin").mkdir(parents=True)
    (existing_backup / "bin" / "server-installer").write_text("older backup", encoding="utf-8")
    _patch_replace(monkeypatch, {
        REPLACE_STAGED_TO_INSTALL: OSError("replacement failure"),
        REPLACE_BACKUP_TO_INSTALL: PermissionError("rollback failure"),
    })

    with pytest.raises(operations.OperationError) as error:
        operations._ensure_server_installer()

    backup_path = error.value.backup_path
    assert backup_path != existing_backup
    assert backup_path.parent == installer_root
    assert (backup_path / "lib" / "previous-module.sh").read_text(encoding="utf-8") == "old installer files"
    assert (existing_backup / "bin" / "server-installer").read_text(encoding="utf-8") == "older backup"
    assert not [path for path in installer_root.iterdir() if path.name.startswith(".v1.1.0-")]


def test_ensure_copies_backup_when_unique_preservation_move_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _install_dir, installer_root = _prepare_ensure_installers(tmp_path, monkeypatch)
    _patch_replace(monkeypatch, {
        REPLACE_STAGED_TO_INSTALL: OSError("private replacement detail"),
        REPLACE_BACKUP_TO_INSTALL: PermissionError("private rollback detail"),
        REPLACE_BACKUP_TO_PREFERRED: OSError("private preferred detail"),
        REPLACE_BACKUP_TO_UNIQUE: OSError("private unique detail"),
    })

    with pytest.raises(operations.OperationError) as error:
        operations._ensure_server_installer()

    message = str(error.value)
    assert "private" not in message
    backup_path = error.value.backup_path
    assert backup_path.parent.parent == installer_root
    assert backup_path.parent.name.startswith("v1.1.0.previous-")
    assert backup_path.name == "v1.1.0"
    assert (backup_path / "lib" / "previous-module.sh").read_text(encoding="utf-8") == "old installer files"
    assert not [path for path in installer_root.iterdir() if path.name.startswith(".v1.1.0-")]


def test_ensure_retains_staging_backup_when_unique_destination_creation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    install_dir, installer_root = _prepare_ensure_installers(tmp_path, monkeypatch)
    replacement_error = OSError("private replacement detail")
    rollback_error = PermissionError("private rollback detail")
    destination_error = OSError("private destination detail")
    _patch_replace(monkeypatch, {
        REPLACE_STAGED_TO_INSTALL: replacement_error,
        REPLACE_BACKUP_TO_INSTALL: rollback_error,
        REPLACE_BACKUP_TO_PREFERRED: OSError("private preferred detail"),
    })
    real_mkdtemp = tempfile.mkdtemp

    def fail_unique_destination(*args, **kwargs):
        if str(kwargs.get("prefix", "")).startswith("v1.1.0.previous-"):
            raise destination_error
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(operations.tempfile, "mkdtemp", fail_unique_destination)

    with pytest.raises(operations.OperationError) as error:
        operations._ensure_server_installer()

    message = str(error.value)
    assert "replacement=OSError" in message
    assert "rollback=PermissionError" in message
    assert "unique path creation=OSError" in message
    assert "private" not in message
    assert isinstance(error.value, operations._InstallerBackupPreservationError)
    backup_path = error.value.backup_path
    assert str(backup_path) in message
    assert backup_path.parent.parent == installer_root
    assert backup_path.parent.name.startswith(".v1.1.0-")
    assert backup_path.name == "previous"
    assert (backup_path / "lib" / "previous-module.sh").read_text(encoding="utf-8") == "old installer files"
    assert error.value.__cause__ is rollback_error
    assert error.value.__context__.__cause__ is destination_error
    assert rollback_error.__context__ is replacement_error
    assert not install_dir.with_name("v1.1.0.previous").exists()
    _assert_operation_error_reports_backup(monkeypatch, error.value, "unique path creation=OSError")


def test_ensure_retains_staging_backup_when_unique_copy_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    install_dir, installer_root = _prepare_ensure_installers(tmp_path, monkeypatch)
    replacement_error = OSError("private replacement detail")
    rollback_error = PermissionError("private rollback detail")
    copy_error = shutil.Error("private copy detail")
    _patch_replace(monkeypatch, {
        REPLACE_STAGED_TO_INSTALL: replacement_error,
        REPLACE_BACKUP_TO_INSTALL: rollback_error,
        REPLACE_BACKUP_TO_PREFERRED: OSError("private preferred detail"),
        REPLACE_BACKUP_TO_UNIQUE: OSError("private unique detail"),
    })

    def fail_copy(source, destination, **kwargs):
        destination = Path(destination)
        destination.mkdir(parents=True)
        (destination / "partial-copy").write_text("partial", encoding="utf-8")
        raise copy_error

    monkeypatch.setattr(operations.shutil, "copytree", fail_copy)

    with pytest.raises(operations.OperationError) as error:
        operations._ensure_server_installer()

    message = str(error.value)
    assert "replacement=OSError" in message
    assert "rollback=PermissionError" in message
    assert "unique move=OSError" in message
    assert "copy=Error" in message
    assert "private" not in message
    assert isinstance(error.value, operations._InstallerBackupPreservationError)
    backup_path = error.value.backup_path
    assert str(backup_path) in message
    assert backup_path.parent.parent == installer_root
    assert backup_path.parent.name.startswith(".v1.1.0-")
    assert backup_path.name == "previous"
    assert (backup_path / "lib" / "previous-module.sh").read_text(encoding="utf-8") == "old installer files"
    assert error.value.__cause__ is rollback_error
    assert error.value.__context__.__cause__ is copy_error
    assert rollback_error.__context__ is replacement_error
    assert not [path for path in installer_root.iterdir() if path.name.startswith("v1.1.0.previous-")]
    assert not install_dir.with_name("v1.1.0.previous").exists()
    _assert_operation_error_reports_backup(monkeypatch, error.value, "unique move=OSError", "copy=Error")


def test_ensure_reports_staging_cleanup_failure_after_successful_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    install_dir, installer_root = _prepare_ensure_installers(tmp_path, monkeypatch)
    cleanup_error = PermissionError("private cleanup detail")
    real_rmtree = shutil.rmtree
    staging_dirs: list[Path] = []

    def fail_staging_cleanup(path, *args, **kwargs):
        path = Path(path)
        if path.parent == installer_root and path.name.startswith(".v1.1.0-"):
            staging_dirs.append(path)
            raise cleanup_error
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(operations.shutil, "rmtree", fail_staging_cleanup)

    with pytest.raises(
        operations.OperationError,
        match="could not provision jooservices/server-installer.*PermissionError",
    ) as error:
        operations._ensure_server_installer()

    assert error.value.__cause__ is cleanup_error
    assert (install_dir / "bin" / "server-installer").read_text(encoding="utf-8") == "new"
    assert len(staging_dirs) == 1
    assert (staging_dirs[0] / "previous" / "lib" / "previous-module.sh").read_text(encoding="utf-8") == \
        "old installer files"


def test_ensure_reports_cleanup_failure_without_hiding_rollback_recovery_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    install_dir, installer_root = _prepare_ensure_installers(tmp_path, monkeypatch)
    existing_backup = install_dir.with_name("v1.1.0.previous")
    (existing_backup / "bin").mkdir(parents=True)
    (existing_backup / "bin" / "server-installer").write_text("older backup", encoding="utf-8")
    replacement_error = OSError("private replacement detail")
    rollback_error = PermissionError("private rollback detail")
    cleanup_error = PermissionError("private cleanup detail")
    _patch_replace(monkeypatch, {
        REPLACE_STAGED_TO_INSTALL: replacement_error,
        REPLACE_BACKUP_TO_INSTALL: rollback_error,
    })
    real_rmtree = shutil.rmtree

    def fail_staging_cleanup(path, *args, **kwargs):
        path = Path(path)
        if path.parent == installer_root and path.name.startswith(".v1.1.0-"):
            raise cleanup_error
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(operations.shutil, "rmtree", fail_staging_cleanup)

    with pytest.raises(operations.OperationError) as error:
        operations._ensure_server_installer()

    assert isinstance(error.value, operations._InstallerRollbackError)
    assert str(error.value.backup_path).startswith(str(installer_root / "v1.1.0.previous-"))
    assert error.value.backup_path.parent == installer_root
    assert error.value.cleanup_error is cleanup_error
    assert error.value.__cause__ is rollback_error
    assert rollback_error.__context__ is replacement_error
    message = str(error.value)
    assert "replacement=OSError" in message
    assert "rollback=PermissionError" in message
    assert "staging cleanup=PermissionError" in message
    assert "private" not in message
    reported_error = _reported_operation_error(monkeypatch, error.value)
    assert len(reported_error) <= 300
    assert str(error.value.backup_path) in reported_error
    assert "replacement=OSError" in reported_error
    assert "rollback=PermissionError" in reported_error
    assert "staging cleanup=PermissionError" in reported_error
    assert (error.value.backup_path / "lib" / "previous-module.sh").read_text(encoding="utf-8") == \
        "old installer files"
    assert (existing_backup / "bin" / "server-installer").read_text(encoding="utf-8") == "older backup"


def test_ensure_retries_staging_cleanup_for_read_only_installer_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    install_dir, installer_root = _prepare_ensure_installers(tmp_path, monkeypatch)
    readonly_dir = install_dir / "lib" / "readonly"
    readonly_dir.mkdir()
    readonly_file = readonly_dir / "module.sh"
    readonly_file.write_text("old installer data", encoding="utf-8")
    readonly_dir.chmod(0o555)
    real_rmtree = shutil.rmtree
    retries: list[Path] = []

    def inject_readonly_permission_error(path, *args, **kwargs):
        path = Path(path)
        if path.parent == installer_root and path.name.startswith(".v1.1.0-"):
            onerror = kwargs["onexc"]
            failure = PermissionError("simulated read-only cleanup")
            onerror(os.unlink, str(path / "previous" / "lib" / "readonly" / "module.sh"), failure)
            retries.append(path)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(operations.shutil, "rmtree", inject_readonly_permission_error)

    operations._ensure_server_installer()

    assert len(retries) == 1
    assert not retries[0].exists()
    assert (install_dir / "bin" / "server-installer").read_text(encoding="utf-8") == "new"


def test_failed_replacement_restores_the_previous_installer_and_reraises_the_original_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    install_dir, source_dir, staging = _prepare_old_and_new_installers(tmp_path)
    _patch_replace(monkeypatch, {REPLACE_STAGED_TO_INSTALL: OSError("device busy")})

    with pytest.raises(OSError, match="device busy") as error:
        operations._replace_server_installer(install_dir, source_dir, staging)

    assert not isinstance(error.value, operations.OperationError)
    assert (install_dir / "bin" / "server-installer").read_text(encoding="utf-8") == "old"
    assert not install_dir.with_name("v1.1.0.previous").exists()


def test_successful_replacement_keeps_the_backup_in_staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    install_dir, source_dir, staging = _prepare_old_and_new_installers(tmp_path)
    _patch_replace(monkeypatch, {})

    operations._replace_server_installer(install_dir, source_dir, staging)

    assert (install_dir / "bin" / "server-installer").read_text(encoding="utf-8") == "new"
    assert (staging / "previous" / "bin" / "server-installer").read_text(encoding="utf-8") == "old"
