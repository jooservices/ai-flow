"""Tests for console session failure handling and fail-closed behavior."""
from __future__ import annotations

import logging

import pytest

from aiflow.v4.control.login_sessions import session_revoked


def test_session_revoked_fails_closed_when_database_lookup_raises(
    ctx, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Return true and log the failure when the database query encounters an unexpected error."""
    def _raise_database_failure(statement: str, parameters: tuple[object, ...] | None = None) -> None:
        """Simulate an unexpected failure while executing a database query."""
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(ctx.db, "one", _raise_database_failure)
    with caplog.at_level(logging.ERROR):
        assert session_revoked(ctx, "session-identifier") is True

    assert "console session check failed" in caplog.text
