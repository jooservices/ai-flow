"""Sample ticket confirmation: a Jira failure while confirming Intake becomes a 502 with an audit row, never a 500."""
from __future__ import annotations

from aiflow.v4.control.jira import JiraError
from test_setup_wizard import SetupJira, bootstrap, make_client


class UnreachableSetupJira(SetupJira):
    def status(self, key):
        raise JiraError(f"GET /rest/api/3/issue/{key} -> 503: provider internals")


def test_jira_error_while_confirming_intake_is_reported_as_not_confirmed_without_provider_details(ctx):
    client = make_client(ctx)
    _, created = bootstrap(client)
    assert created.status_code == 200
    ctx.jira = UnreachableSetupJira()

    response = client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"})

    assert response.status_code == 502
    assert "could not confirm it in Intake" in response.json()["detail"]
    assert "provider internals" not in response.json()["detail"]
    audit = ctx.db.one("SELECT accepted,result FROM write_audit WHERE target='setup' AND action='sample_ticket'")
    assert audit == {"accepted": False, "result": "sample issue not confirmed in Intake"}
    assert ctx.db.one("SELECT sample_confirmed_at FROM setup_wizard_state WHERE id=true")["sample_confirmed_at"] is None
