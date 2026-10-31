"""The Command Center loads the owner-escalation renderer from its feature asset."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiflow.v4.control.ui import mount_ui
from aiflow.v4.control.ui import console_template


def test_owner_escalation_panel_asset_is_served_and_guides_ticket_answers_in_jira():
    app = FastAPI()
    mount_ui(app, object())
    page = console_template()
    with TestClient(app) as client:
        response = client.get("/console-assets/owner-escalation.js")

    assert response.status_code == 200
    assert '<script src="/console-assets/owner-escalation.js"></script>' in page
    assert "Answer in Jira with a comment after the question, then move the ticket to To Do." in response.text
    assert "if (writer && decision.jira_answer_required)" in response.text
    assert "data-act=\"esc-answer\"" in response.text
