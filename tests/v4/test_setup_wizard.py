"""First-run wizard: browser bootstrap, checked setup steps, skip/resume and Intake completion."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aiflow.v4.control import setup_wizard
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import COOKIE_NAME
from aiflow.v4.control.ui import mount_ui
from conftest import FakeGitHub, FakeJira

SECRET = "setup-wizard-session-secret-with-32-bytes"
EMAIL = "First.Owner@Example.Test"
PASSWORD = "a-new-browser-password-for-ai-flow"


def make_client(ctx):
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SECRET
    ctx.settings.public_url = "https://initial.example.test"
    ctx.jira = None
    ctx.github = None
    app = create_app(ctx)
    mount_ui(app, ctx)
    app.state.sync_accounts()
    return TestClient(app, follow_redirects=False, client=("127.0.0.1", 50000))


def status(client):
    response = client.get("/v1/setup/status")
    assert response.status_code == 200, response.text
    return response.json()


def by_id(data, step):
    return next(item for item in data["steps"] if item["id"] == step)


def bootstrap(client, url="http://console.example.test"):
    challenge = status(client)
    response = client.post("/v1/setup/bootstrap", json={"public_url": url, "email": EMAIL,
                                  "password": PASSWORD},
                           headers={"X-AIFLOW4-Setup": challenge["bootstrap_token"]})
    return challenge, response


class SetupJira(FakeJira):
    def __init__(self):
        super().__init__()
        self.created: list[tuple[str, str, str, str, list[str]]] = []
        self.transition_fails = False

    def create_issue(self, project, summary, description, issuetype="Story", labels=None, **_kwargs):
        key = f"{project}-99"
        self.created.append((project, summary, description, issuetype, list(labels or [])))
        self.status_of[key] = "To Do"
        return key

    def transition_to(self, key, target):
        self.transitions.append((key, target))
        if self.transition_fails:
            return self.status_of.get(key, "To Do")
        self.status_of[key] = target
        return target


class SetupJiraAuth:
    def __init__(self, connected):
        self.connected = connected

    def available(self):
        return self.connected


def test_first_start_serves_setup_and_bootstraps_public_address_and_administrator_in_browser(ctx):
    client = make_client(ctx)
    setup_page = client.get("/login")
    assert setup_page.status_code == 200
    assert "Set up AI Flow" in setup_page.text and "Create Administrator" in setup_page.text
    assert setup_wizard.user_count(ctx) == 0

    before = status(client)
    assert before["admin_exists"] is False
    assert by_id(before, "public_address")["missing"] == "Choose the address people use to open AI Flow."
    assert by_id(before, "jira")["missing"] == "Connect a Jira account in Integrations."
    no_nonce = client.post("/v1/setup/bootstrap", json={"public_url": "http://console.example.test",
                            "email": EMAIL, "password": PASSWORD})
    assert no_nonce.status_code == 403

    bad_password = client.post("/v1/setup/bootstrap", json={"public_url": "http://console.example.test",
                                  "email": EMAIL, "password": "short"},
                               headers={"X-AIFLOW4-Setup": before["bootstrap_token"]})
    assert bad_password.status_code == 422 and setup_wizard.user_count(ctx) == 0

    challenge, created = bootstrap(client)
    assert challenge["bootstrap_token"] and created.status_code == 200, created.text
    assert created.json() == {"created": True, "email": "first.owner@example.test", "next": "/setup"}
    assert COOKIE_NAME in created.headers["set-cookie"]
    user = ctx.db.one("SELECT email,role,roles,password_hash FROM console_users")
    assert user["email"] == "first.owner@example.test" and user["role"] == "administrator"
    assert user["roles"] == ["administrator"] and PASSWORD not in user["password_hash"]
    assert ctx.db.one("SELECT public_url FROM setup_wizard_state WHERE id=true")["public_url"] == "http://console.example.test"
    assert ctx.settings.public_url == "http://console.example.test"
    assert ctx.settings.join_control_url == "https://control.example.test"
    audit = ctx.db.one("SELECT payload::text AS payload FROM write_audit WHERE target='setup' AND action='bootstrap'")
    assert PASSWORD not in audit["payload"]
    assert client.get("/v1/auth/me").status_code == 200  # bootstrap signs the owner in immediately
    assert client.get("/setup").status_code == 200


def test_each_external_step_reports_missing_action_and_skip_can_be_resumed(ctx):
    client = make_client(ctx)
    # The shared control-repo fixture syncs its sample fleet when the UI mounts.
    ctx.db.run("DELETE FROM accounts")
    ctx.db.run("DELETE FROM nodes")
    _, created = bootstrap(client)
    assert created.status_code == 200
    data = status(client)
    assert by_id(data, "administrator")["ready"] is True
    assert by_id(data, "jira")["missing"] == "Connect a Jira account in Integrations."
    assert by_id(data, "github")["missing"] == "Connect a GitHub App or an existing App in Integrations."
    assert by_id(data, "jira")["ready"] is False
    assert by_id(data, "machine")["missing"] == "Add a machine and use its one-time join command."
    assert by_id(data, "ai_accounts")["missing"] == "Add an AI account to a machine and finish its sign-in."
    assert by_id(data, "project")["ready"] is True  # TESTWP is already in the control-repo fixture
    assert by_id(data, "sample_ticket")["missing"].startswith("Choose a configured project")

    skipped = client.post("/v1/setup/steps/jira", json={"skipped": True})
    assert skipped.status_code == 200 and by_id(skipped.json(), "jira")["skipped"] is True
    assert by_id(skipped.json(), "jira")["ready"] is False
    resumed = client.post("/v1/setup/steps/jira", json={"skipped": False})
    assert resumed.status_code == 200 and by_id(resumed.json(), "jira")["skipped"] is False
    assert client.post("/v1/setup/steps/administrator", json={"skipped": True}).status_code == 404


def test_integrations_machine_and_account_checks_follow_live_connections(ctx):
    client = make_client(ctx)
    _, created = bootstrap(client)
    assert created.status_code == 200
    ctx.jira = FakeJira()
    ctx.jira_auth = SetupJiraAuth(False)
    assert by_id(status(client), "jira")["ready"] is False
    assert client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"}).status_code == 409
    ctx.jira_auth = SetupJiraAuth(True)
    ctx.github = FakeGitHub()
    ctx.db.run("INSERT INTO nodes (name,role,status) VALUES ('setup-node','worker','joined')")
    ctx.db.run("INSERT INTO accounts (id,provider,node,status) VALUES ('codex-setup','codex','setup-node','signed_out')")
    data = status(client)
    assert by_id(data, "jira")["ready"] is True
    assert by_id(data, "github")["ready"] is True
    assert by_id(data, "machine")["ready"] is True
    assert by_id(data, "ai_accounts")["ready"] is False  # assigned, but its sign-in has not passed
    ctx.db.run("UPDATE accounts SET status='idle' WHERE id='codex-setup'")
    assert by_id(status(client), "ai_accounts")["ready"] is True


def test_sample_ticket_completes_only_after_jira_confirms_intake_and_then_locks_wizard(ctx):
    client = make_client(ctx)
    _, created = bootstrap(client)
    assert created.status_code == 200
    jira = SetupJira()
    ctx.jira = jira

    sample = client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"})
    assert sample.status_code == 200, sample.text
    assert sample.json()["ticket"] == "TESTWP-99" and sample.json()["status"] == "Intake"
    assert sample.json()["completed"] is True
    assert jira.created[0][0] == "TESTWP" and "aiflow-setup-sample" in jira.created[0][4]
    assert jira.transitions == [("TESTWP-99", "Intake")]
    current = status(client)
    assert current["completed"] is True and current["locked"] is True
    assert by_id(current, "sample_ticket")["ready"] is True
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-99'")["status"] == "Intake"
    assert client.get("/setup").status_code == 303 and client.get("/setup").headers["location"] == "/dashboard"
    assert client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"}).status_code == 409
    assert client.post("/v1/setup/steps/jira", json={"skipped": True}).status_code == 409

    console = (Path(__file__).resolve().parents[2] / "aiflow/v4/control/ui/console.html").read_text()
    assert "setupDashboardPanel" in console and "Continue setup" in console
    assert "data-view=\"integrations\"" in console and "data-view=\"machines\"" in console
    assert "data-view=\"projects\"" in console


def test_sample_ticket_failure_says_it_was_not_confirmed_and_keeps_setup_open(ctx):
    client = make_client(ctx)
    _, created = bootstrap(client)
    assert created.status_code == 200
    jira = SetupJira()
    jira.transition_fails = True
    ctx.jira = jira
    response = client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"})
    assert response.status_code == 502
    assert "could not confirm it in Intake" in response.json()["detail"]
    assert "TESTWP-99" in response.json()["detail"]
    current = status(client)
    assert current["completed"] is False
    assert by_id(current, "sample_ticket")["ready"] is False
    assert by_id(current, "sample_ticket")["missing"].startswith("Choose a configured project")
