"""A notification failure after a project report is saved does not fail the close."""
from __future__ import annotations

from aiflow.v4.control import notify


def test_a_notification_exception_keeps_the_saved_report_and_still_closes_the_project(client, ctx, monkeypatch, caplog):
    """Answer 200 with no sent channels, record them as empty, and emit the report event when notify.send raises."""
    def failing_send(*_args, **_kwargs):
        raise RuntimeError("simulated notification outage")

    monkeypatch.setattr(notify, "send", failing_send)

    response = client.post("/v1/projects/TESTWP/close", json={"actor": "lead"})

    assert response.status_code == 200
    assert response.json()["status"] == "closed" and response.json()["sent_channels"] == []
    assert ctx.db.one("SELECT sent_channels FROM project_reports WHERE scope='project'") == {"sent_channels": []}
    assert ctx.db.one("SELECT status FROM project_lifecycle WHERE project='TESTWP'") == {"status": "closed"}
    event = ctx.db.one("SELECT reason FROM events WHERE type='project.report_generated'")
    assert event == {"reason": "TESTWP:v1 sent via no configured channel"}
    assert "project report notification failed for TESTWP" in caplog.text
