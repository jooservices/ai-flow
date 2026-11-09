"""Background drops retry transient database errors and report every database left behind."""
from __future__ import annotations

from collections import Counter

import psycopg
import pytest

from aiflow.v4.core import testdb


def test_background_dropper_retries_and_reports_every_remaining_database(monkeypatch, caplog):
    attempts: Counter[str] = Counter()

    def drop(_admin_url: str, name: str) -> None:
        attempts[name] += 1
        if name == "transient" and attempts[name] == 1:
            raise psycopg.OperationalError("temporary database outage")
        if name != "transient":
            raise psycopg.OperationalError("database unavailable")

    monkeypatch.setattr(testdb, "drop", drop)
    monkeypatch.setattr(testdb, "DROP_RETRY_DELAY_S", 0)
    dropper = testdb.BackgroundDropper("postgresql://unused")
    for name in ("transient", "stuck_one", "stuck_two"):
        dropper.submit(name)

    with pytest.raises(RuntimeError, match="stuck_one, stuck_two") as error:
        dropper.wait()

    assert attempts == {"transient": 2, "stuck_one": testdb.DROP_ATTEMPTS, "stuck_two": testdb.DROP_ATTEMPTS}
    assert "stuck_one" in caplog.text and "stuck_two" in caplog.text
    assert "transient" not in str(error.value)
