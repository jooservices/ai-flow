"""Notification failure handling for master escalation delivery."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from aiflow.v4.control.features.runs.bodies import MasterActionBody
from aiflow.v4.control.features.runs.master_actions import (
    EscalationRecord,
    MasterActionState,
    deliver_escalation,
)


def test_escalation_delivery_failure_logs_warning_and_preserves_pending_channels(monkeypatch, caplog):
    """Log a warning and keep pending channels for retry when notification dispatch fails."""
    def _failing_send(*_args, **_kwargs):
        raise RuntimeError("notification gateway offline")

    monkeypatch.setattr("aiflow.v4.control.notify.send", _failing_send)

    ctx = SimpleNamespace(settings=SimpleNamespace(jira_site="https://jira.example.com"))
    body = MasterActionBody(run_id="run-1", fence=1, action="escalate_to_owner", intent="Need owner approval")
    state = MasterActionState(run={"account_id": "account-1"}, node="node-1", policy_project="TEST",
                              ticket="TEST-1", body=body)
    record = EscalationRecord(
        created_row=True,
        reason="owner decision required",
        options=("option1", "option2"),
        intent="Need owner approval",
        configured_channels=("email", "slack"),
        pending_channels=("email", "slack"),
        delivered_channels=(),
    )
    logger = logging.getLogger("aiflow4.test_master_actions")

    with caplog.at_level(logging.WARNING):
        delivery = deliver_escalation(ctx, state, record, "TEST", logger)

    assert delivery.sent == ()
    assert delivery.pending_channels == ("email", "slack")
    assert delivery.delivered_channels == ()
    assert not delivery.already_sent
    assert "master escalation notification failed: RuntimeError" in caplog.text
