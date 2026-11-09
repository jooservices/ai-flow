"""Failure handling of the optional extras that a work claim builds from external services."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from aiflow.v4.control.features.work import claim_extras, claim_steps
from aiflow.v4.control.jira import JiraError
from tests.v4.test_claim_steps import _body, _ticket


def test_failed_comment_lookup_returns_no_comments_and_logs_a_warning(monkeypatch, caplog):
    def jira_is_down(*_args, **_kwargs):
        raise JiraError("GET /rest/api/3/issue/TESTWP-CLAIM/comment -> 503")

    store = SimpleNamespace(intake_wording_refusals=lambda _key: 0, intake_technical_answers=lambda _key: 0)
    ctx = SimpleNamespace(jira=SimpleNamespace(recent_comments=jira_is_down))
    work = claim_steps.Claim(ctx=ctx, deps=None, body=_body(), node="cc", store=store, ticket=_ticket())

    with caplog.at_level(logging.WARNING, logger="aiflow4.control"):
        extras = claim_extras.intake_extras(work)

    assert extras == {}
    assert "comments of TESTWP-CLAIM for intake failed" in caplog.text
