from __future__ import annotations

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
