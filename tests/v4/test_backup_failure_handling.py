"""Failure handling of the backup writer (JAIF-327): an interrupted backup keeps the old file and leaves no partial file."""
from __future__ import annotations

import os

import pytest

from aiflow.v4.install import backup


def test_interrupted_backup_removes_the_partial_file_and_keeps_the_existing_destination(
    test_database_url, tmp_path, monkeypatch
):
    out = tmp_path / "backup.tar.gz"
    out.write_bytes(b"previous backup")

    def interrupted_write(fd, database_url, lay):
        os.close(fd)
        raise KeyboardInterrupt

    monkeypatch.setattr(backup, "_write", interrupted_write)

    with pytest.raises(KeyboardInterrupt):
        backup.create(test_database_url, out)

    assert out.read_bytes() == b"previous backup"
    assert list(tmp_path.glob(".backup.tar.gz.*")) == []
