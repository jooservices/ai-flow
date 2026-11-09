from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiflow.v4.node import operations

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
    assert "replacement failed (OSError)" in message
    assert "restoring the previous installer failed (PermissionError)" in message
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
    assert "replacement failed (OSError)" in message
    assert "restoring the previous installer failed (PermissionError)" in message
    assert "private" not in message
    backup_path = Path(message.rsplit(" ", 1)[-1])
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

    backup_path = Path(str(error.value).rsplit(" ", 1)[-1])
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
    backup_path = Path(message.rsplit(" ", 1)[-1])
    assert backup_path.parent.parent == installer_root
    assert backup_path.parent.name.startswith("v1.1.0.previous-")
    assert backup_path.name == "v1.1.0"
    assert (backup_path / "lib" / "previous-module.sh").read_text(encoding="utf-8") == "old installer files"
    assert not [path for path in installer_root.iterdir() if path.name.startswith(".v1.1.0-")]


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
