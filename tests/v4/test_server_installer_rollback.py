from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiflow.v4.node import operations

# Numbers of the os.replace calls in _replace_server_installer, in order: source to staged, install to backup,
# staged to install, backup to install (the restore), backup to the kept path.
REPLACE_STAGED_TO_INSTALL = 3
REPLACE_BACKUP_TO_INSTALL = 4


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
