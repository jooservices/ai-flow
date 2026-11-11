from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aiflow.v4.worker import quota_cli
from aiflow.v4.worker.quota_cli import read_quota

NO_COMMAND_SENT = "provider CLI did not show its interactive prompt; no quota command was sent"


def test_quota_cli_child_exits_quietly_when_the_provider_binary_is_missing(tmp_path, monkeypatch):
    empty_bin_dir = tmp_path / "empty-bin"
    empty_bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin_dir))

    quota, error, observed_at = read_quota("codex", str(tmp_path), run_as=[], timeout_s=2,
                                           start_delay_s=.01, response_wait_s=.05)

    assert quota == {} and observed_at is None
    assert error == NO_COMMAND_SENT


def test_quota_cli_child_exits_quietly_when_the_exec_arguments_have_the_wrong_type(tmp_path):
    quota, error, observed_at = read_quota("codex", str(tmp_path), run_as=[1], timeout_s=2,
                                           start_delay_s=.01, response_wait_s=.05)

    assert quota == {} and observed_at is None
    assert error == NO_COMMAND_SENT


def test_quota_cli_child_exits_instead_of_resuming_its_caller_on_an_unexpected_startup_error(tmp_path):
    script = """
import os, sys
from aiflow.v4.worker import quota_cli
home = sys.argv[1]
os.environ["PATH"] = "/no-provider-binaries"
def reject_child_chdir(event, args):
    if event == "os.chdir" and args and args[0] == home:
        raise RuntimeError("startup sentinel")
sys.addaudithook(reject_child_chdir)
quota_cli.read_quota("codex", home, timeout_s=1, start_delay_s=.01, response_wait_s=.05)
os.write(1, b"caller-ran\\n")
"""
    repository = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.stdout == "caller-ran\n"


def test_quota_cli_child_exits_after_an_unexpected_startup_error(monkeypatch, tmp_path):
    exit_codes = []

    def fail_to_change_directory(account_home: str) -> None:
        """Raise an unexpected startup error for the child-exit guard."""
        raise RuntimeError("startup sentinel")

    def record_child_exit(exit_code: int) -> None:
        """Record the exit status while stopping the simulated fork child."""
        exit_codes.append(exit_code)
        raise SystemExit(exit_code)

    monkeypatch.setattr(quota_cli.pty, "fork", lambda: (0, 17))
    monkeypatch.setattr(quota_cli.os, "chdir", fail_to_change_directory)
    monkeypatch.setattr(quota_cli.os, "_exit", record_child_exit)

    with pytest.raises(SystemExit) as exited:
        read_quota("codex", str(tmp_path), run_as=[])

    assert exited.value.code == 127
    assert exit_codes == [127]
