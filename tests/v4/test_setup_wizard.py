"""First-run wizard: browser bootstrap, checked setup steps, skip/resume and Intake completion."""
from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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
OWNERSHIP_CAPABILITY = "first-run-ownership-capability-with-over-32-characters"


def make_client(ctx, *, client_address=("127.0.0.1", 50000), base_url="https://testserver"):
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = SECRET
    ctx.settings.setup_ownership_capability = OWNERSHIP_CAPABILITY
    ctx.settings.public_url = "https://initial.example.test"
    ctx.jira = None
    ctx.github = None
    app = create_app(ctx)
    mount_ui(app, ctx)
    app.state.sync_accounts()
    return TestClient(app, follow_redirects=False, base_url=base_url, client=client_address)


def status(client):
    response = client.get("/v1/setup/status")
    assert response.status_code == 200, response.text
    return response.json()


def by_id(data, step):
    return next(item for item in data["steps"] if item["id"] == step)


def bootstrap(client, url="https://console.example.test"):
    challenge = status(client)
    response = client.post("/v1/setup/bootstrap", json={"public_url": url, "email": EMAIL,
                                  "password": PASSWORD},
                           headers={"X-AIFLOW4-Setup": challenge["bootstrap_token"],
                                    "X-AIFLOW4-Ownership": OWNERSHIP_CAPABILITY})
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


class SlowSetupJira(SetupJira):
    def create_issue(self, *args, **kwargs):
        time.sleep(0.15)
        return super().create_issue(*args, **kwargs)


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
    assert set(before) == {"admin_exists", "locked", "bootstrap_token"}
    assert "ownership_capability" not in before and "projects" not in before and "steps" not in before
    assert "TESTWP" not in str(before)
    no_nonce = client.post("/v1/setup/bootstrap", json={"public_url": "https://console.example.test",
                            "email": EMAIL, "password": PASSWORD})
    assert no_nonce.status_code == 403

    wrong_owner = client.post("/v1/setup/bootstrap", json={"public_url": "https://console.example.test",
                                "email": EMAIL, "password": PASSWORD},
                              headers={"X-AIFLOW4-Setup": before["bootstrap_token"],
                                       "X-AIFLOW4-Ownership": "not-the-installation-capability"})
    assert wrong_owner.status_code == 403 and setup_wizard.user_count(ctx) == 0
    assert ctx.db.one("SELECT ownership_claimed_at FROM setup_wizard_state WHERE id=true")["ownership_claimed_at"] is None

    bad_password = client.post("/v1/setup/bootstrap", json={"public_url": "https://console.example.test",
                                  "email": EMAIL, "password": "short"},
                               headers={"X-AIFLOW4-Setup": before["bootstrap_token"],
                                        "X-AIFLOW4-Ownership": OWNERSHIP_CAPABILITY})
    assert bad_password.status_code == 422 and setup_wizard.user_count(ctx) == 0

    challenge, created = bootstrap(client)
    assert challenge["bootstrap_token"] and created.status_code == 200, created.text
    assert created.json() == {"created": True, "email": "first.owner@example.test", "next": "/dashboard"}
    assert COOKIE_NAME in created.headers["set-cookie"]
    user = ctx.db.one("SELECT email,role,roles,password_hash FROM console_users")
    assert user["email"] == "first.owner@example.test" and user["role"] == "administrator"
    assert user["roles"] == ["administrator"] and PASSWORD not in user["password_hash"]
    assert ctx.db.one("SELECT public_url FROM setup_wizard_state WHERE id=true")["public_url"] == "https://console.example.test"
    assert ctx.db.one("SELECT ownership_claimed_at FROM setup_wizard_state WHERE id=true")["ownership_claimed_at"] is not None
    assert ctx.settings.public_url == "https://console.example.test"
    assert ctx.settings.join_control_url == "https://control.example.test"
    audit = ctx.db.one("SELECT payload::text AS payload FROM write_audit WHERE target='setup' AND action='bootstrap'")
    assert PASSWORD not in audit["payload"]
    assert client.get("/v1/auth/me").status_code == 200  # bootstrap signs the owner in immediately
    # AC5: the wizard screen is locked as soon as the first Administrator exists, before any step is done.
    locked = client.get("/setup")
    assert locked.status_code == 303 and locked.headers["location"] == "/dashboard"
    assert status(client)["locked"] is True and status(client)["completed"] is False


def test_remote_bootstrap_requires_https_but_local_development_can_use_http(ctx):
    remote_http = make_client(ctx, client_address=("198.51.100.25", 50000), base_url="http://testserver")
    challenge = status(remote_http)
    response = remote_http.post("/v1/setup/bootstrap", json={"public_url": "https://console.example.test",
                                     "email": EMAIL, "password": PASSWORD},
                               headers={"X-AIFLOW4-Setup": challenge["bootstrap_token"],
                                        "X-AIFLOW4-Ownership": OWNERSHIP_CAPABILITY})
    assert response.status_code == 400
    assert "HTTPS" in response.json()["detail"]
    assert setup_wizard.user_count(ctx) == 0

    for path in ("/setup", "/login"):
        refused = remote_http.get(path)
        assert refused.status_code == 400 and "HTTPS" in refused.text and "<form" not in refused.text

    remote_https = make_client(ctx, client_address=("198.51.100.25", 50001), base_url="https://testserver")
    assert remote_https.get("/setup").status_code == 200 and remote_https.get("/login").status_code == 200
    challenge = status(remote_https)
    http_address = remote_https.post("/v1/setup/bootstrap", json={"public_url": "http://console.example.test",
                                      "email": EMAIL, "password": PASSWORD},
                                     headers={"X-AIFLOW4-Setup": challenge["bootstrap_token"],
                                              "X-AIFLOW4-Ownership": OWNERSHIP_CAPABILITY})
    assert http_address.status_code == 422 and "https://" in http_address.json()["detail"]
    assert setup_wizard.user_count(ctx) == 0
    _, created = bootstrap(remote_https, url="https://console.example.test")
    assert created.status_code == 200, created.text
    assert "secure" in created.headers["set-cookie"].lower()


def test_remote_http_address_is_refused_even_from_a_loopback_client(ctx):
    client = make_client(ctx)
    for url in ("http://console.example.test", "http://203.0.113.9:8080", "http://localhost.example.test"):
        _, refused = bootstrap(client, url=url)
        assert refused.status_code == 422 and "https://" in refused.json()["detail"]
    assert setup_wizard.user_count(ctx) == 0
    _, created = bootstrap(client, url="http://localhost:8080")
    assert created.status_code == 200, created.text


def test_machine_step_waits_for_the_first_project_and_points_to_projects(ctx, monkeypatch):
    from aiflow.v4.control import setup_service

    client = make_client(ctx)
    _, created = bootstrap(client)
    assert created.status_code == 200
    ids = [step["id"] for step in status(client)["steps"]]
    assert ids.index("project") < ids.index("machine") < ids.index("ai_accounts")
    monkeypatch.setattr(setup_service, "_projects", lambda c: [], raising=False)
    assert setup_service._action("machine", False)["url"] == "/projects"
    assert setup_service._action("machine", True)["url"] == "/machines"
    assert "Projects" in setup_service.MACHINE_NEEDS_PROJECT


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
    # Jira, GitHub, machine and AI accounts are still missing, so setup is not complete yet (AC3).
    assert sample.json()["completed"] is False
    assert jira.created[0][0] == "TESTWP" and "aiflow-setup-sample" in jira.created[0][4]
    assert jira.transitions == [("TESTWP-99", "Intake")]
    current = status(client)
    assert current["completed"] is False and current["locked"] is True
    assert by_id(current, "sample_ticket")["ready"] is True
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-99'")["status"] == "Intake"
    assert client.get("/setup").status_code == 303 and client.get("/setup").headers["location"] == "/dashboard"

    # Skipped steps stay resumable from the Dashboard after the sample ticket was sent.
    skipped = client.post("/v1/setup/steps/jira", json={"skipped": True})
    assert skipped.status_code == 200 and by_id(skipped.json(), "jira")["skipped"] is True
    resumed = client.post("/v1/setup/steps/jira", json={"skipped": False})
    assert resumed.status_code == 200 and by_id(resumed.json(), "jira")["skipped"] is False

    # Once every step is ready the Dashboard panel goes away.
    ctx.jira_auth = SetupJiraAuth(True)
    ctx.github = FakeGitHub()
    ctx.db.run("INSERT INTO nodes (name,role,status) VALUES ('done-node','worker','joined')")
    ctx.db.run("INSERT INTO accounts (id,provider,node,status) VALUES ('codex-done','codex','done-node','idle')")
    assert status(client)["completed"] is True

    ui = Path(__file__).resolve().parents[2] / "aiflow/v4/control/ui"
    console = (ui / "console.html").read_text()
    assert "/ui/setup-progress.js" in console and "AIFlowSetup.panel()" in console
    # the panel script is added at run time, never through a static tag
    assert '<script src="/ui/setup-progress.js"' not in console and "src=\"/ui/setup-progress.js\"" not in console
    assert "data-view=\"integrations\"" in console and "data-view=\"machines\"" in console
    assert "data-view=\"projects\"" in console
    script = client.get("/ui/setup-progress.js")
    assert script.status_code == 200 and "Skip for now" in script.text and "Resume this step" in script.text
    assert 'data-block="first-run-setup"' in console
    setup_page = client.get("/setup")
    assert setup_page.status_code == 303


def test_concurrent_sample_ticket_requests_create_only_one_jira_issue(ctx):
    client = make_client(ctx)
    _, created = bootstrap(client)
    assert created.status_code == 200
    jira = SlowSetupJira()
    ctx.jira = jira
    start = Barrier(2)

    def send_sample():
        start.wait(timeout=5)
        return client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: send_sample(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(jira.created) == 1 and jira.transitions == [("TESTWP-99", "Intake")]
    assert ctx.db.one("SELECT sample_ticket_key FROM setup_wizard_state WHERE id=true")["sample_ticket_key"] == "TESTWP-99"


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


def test_sample_ticket_stays_confirmed_after_the_flow_moves_it_and_is_never_re_transitioned(ctx):
    client = make_client(ctx)
    _, created = bootstrap(client)
    assert created.status_code == 200
    jira = SetupJira()
    ctx.jira = jira
    assert client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"}).status_code == 200
    ctx.jira_auth = SetupJiraAuth(True)
    ctx.github = FakeGitHub()
    ctx.db.run("INSERT INTO nodes (name,role,status) VALUES ('move-node','worker','joined')")
    ctx.db.run("INSERT INTO accounts (id,provider,node,status) VALUES ('codex-move','codex','move-node','idle')")
    assert status(client)["completed"] is True


    # The flow picks the sample up: Jira no longer reports Intake.
    for moved in ("To Do", "In Progress"):
        jira.status_of["TESTWP-99"] = moved
        current = status(client)
        assert current["completed"] is True
        assert by_id(current, "sample_ticket")["ready"] is True and by_id(current, "sample_ticket")["missing"] == ""
    assert ctx.db.one("SELECT completed_at FROM setup_wizard_state WHERE id=true")["completed_at"] is not None

    # A second send is refused and never drags the live ticket back to Intake.
    again = client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"})
    assert again.status_code == 409
    assert jira.transitions == [("TESTWP-99", "Intake")] and len(jira.created) == 1
    assert jira.status_of["TESTWP-99"] == "In Progress"

    # Completion is sticky even when a later condition changes, and the poll makes no Jira call.
    ctx.db.run("DELETE FROM accounts WHERE id='codex-move'")
    jira.status = lambda key: (_ for _ in ()).throw(AssertionError("status polling must not call Jira"))
    assert status(client)["completed"] is True


def test_dashboard_setup_panel_disappears_after_completion():
    script = Path(__file__).resolve().parents[2] / "aiflow/v4/control/ui/setup-progress.js"
    browser_check = r"""
const fs = require('node:fs');
const status = { completed: false, steps: [
  { id: 'public_address', title: 'Public address', ready: true, missing: '', skipped: false },
  { id: 'administrator', title: 'Administrator', ready: true, missing: '', skipped: false },
  { id: 'jira', title: 'Connect Jira', ready: false, missing: 'Connect Jira in Integrations.', skipped: false }
] };
const panel = { innerHTML: '' };
global.window = {};
global.S = { consoleUser: {}, view: 'cc', data: {} };
global.consoleCan = () => true;
global.document = { addEventListener: () => {}, querySelector: () => panel };
global.renderDashboard = () => { panel.innerHTML = window.AIFlowSetup.panel(); };
global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(status) });
eval(fs.readFileSync(process.argv[1], 'utf8'));
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
(async () => {
  for (let i = 0; i < 10 && !panel.innerHTML; i += 1) await tick();
  if (!panel.innerHTML.includes('Finish setting up AI Flow')) process.exitCode = 1;
  status.completed = true;
  window.AIFlowSetup.poll(true);
  for (let i = 0; i < 10 && panel.innerHTML; i += 1) await tick();
  if (panel.innerHTML !== '') process.exitCode = 2;
})().catch(() => { process.exitCode = 3; });
"""
    result = subprocess.run(["node", "-e", browser_check, str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or f"Dashboard panel assertion exited {result.returncode}"


def test_second_send_before_other_steps_are_done_does_not_re_transition(ctx):
    client = make_client(ctx)
    _, created = bootstrap(client)
    assert created.status_code == 200
    jira = SetupJira()
    ctx.jira = jira
    assert client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"}).status_code == 200
    jira.status_of["TESTWP-99"] = "In Progress"
    again = client.post("/v1/setup/sample-ticket", json={"project": "TESTWP"})
    assert again.status_code == 409 and "already sent" in again.json()["detail"]
    assert jira.transitions == [("TESTWP-99", "Intake")]
    assert by_id(status(client), "sample_ticket")["ready"] is True
