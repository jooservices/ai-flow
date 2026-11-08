from __future__ import annotations

import logging

from aiflow.v4.control import flow_doctor
from tests.v4.conftest import add_ticket


def test_a_failed_jira_link_read_is_logged_and_the_dependency_waits_for_the_next_pass(ctx, monkeypatch, caplog):
    add_ticket(ctx.db, "TESTWP-2671", status="Backlog")
    add_ticket(ctx.db, "TESTWP-2672", blocked_by=("TESTWP-2671",))

    def unreachable_links(key: str):
        raise RuntimeError("Jira token refresh failed")

    monkeypatch.setattr(ctx.jira, "get_issue_links", unreachable_links)
    with caplog.at_level(logging.WARNING, logger="aiflow4.flow_doctor"):
        flow_doctor._fix_impossible_dependencies(ctx)

    assert "doctor could not read Jira links for TESTWP-2672 (RuntimeError)" in caplog.text
    assert ctx.db.one("SELECT blocked_by FROM tickets WHERE key='TESTWP-2672'")["blocked_by"] == ["TESTWP-2671"]
    assert ctx.db.one("SELECT count(*) AS total FROM flow_doctor_actions WHERE ticket_key='TESTWP-2672'") == {"total": 0}
