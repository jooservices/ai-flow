"""Failure handling of the provider sign-in error path when its own status report fails."""
from __future__ import annotations

import threading

from aiflow.v4.node import provider_sign_in
from tests.v4.test_provider_sign_in_run_parity import _APIRecorder


def test_error_helper_still_logs_the_warning_when_the_status_report_fails(monkeypatch):
    api = _APIRecorder()
    logs = []

    def unreachable(method, path, body=None, **kwargs):
        raise RuntimeError("control API unreachable")

    monkeypatch.setattr(api, "call", unreachable)
    monkeypatch.setattr(provider_sign_in.log, "warning", lambda *args: logs.append(args))
    provider_sign_in._handle_provider_sign_in_error(
        api, "node-01", "node-01", "req-01", False, threading.Event(), OSError("private detail"))
    assert logs == [("provider sign-in failed for %s (%s)", "node-01", "OSError")]
