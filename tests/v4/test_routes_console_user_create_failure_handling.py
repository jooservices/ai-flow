"""An unexpected failure while adding a console person is logged and answered with a fixed 500 message."""
from __future__ import annotations

from test_console_user_audit_events import PERSON, admin_client


def test_an_unexpected_failure_adding_a_person_returns_500_and_logs_it(ctx, monkeypatch, caplog):
    client = admin_client(ctx)

    def fail_publish(*args, **kwargs):
        raise RuntimeError("forced event failure")

    monkeypatch.setattr("aiflow.v4.control.features.console.routes.publish_for", fail_publish)
    refused = client.post("/v1/console/users", json={"email": PERSON, "display_name": "New Person",
                                                    "roles": ["lead"], "project_keys": []})
    assert refused.status_code == 500
    assert refused.json()["detail"] == "person could not be added"
    assert [(record.levelname, record.getMessage()) for record in caplog.records if record.name == "aiflow4.api"] == [
        ("ERROR", "console user could not be added")]
