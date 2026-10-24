"""Clean-install acceptance test (JAIF-248, docs/v4/clean-install-acceptance.md).

The fast tier: a clean control machine and two clean machines are built from the release with console sign-in on,
first-run setup follows only the documented steps (install, sign in as the Super Administrator, add a project, join
machines), and the first tickets travel through Intake, development, verification and merge. A second ticket that
needs a human reaches the Human review screen. The same sign-in gate then has to let the machines' own calls and the
deploy scripts through, and backup and restore, upgrade and rollback, adding a machine and an AI account work.

The control host, Jira and GitHub are the fakes of the other v4 tests; the release-signed systemd tiers (Ubuntu
containers) are `deploy/v4/ci/clean-install-containers.sh` and `control-install-smoke.sh`, driven by
`deploy/v4/ci/clean-install-acceptance.sh full` or the tagged release workflow.

Every problem found later in real use gets a case in REGRESSIONS at the end (AC5).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from test_control_install import ROOT, FakeHost, console_seeds, installer, machine  # noqa: F401 - `machine` is a fixture
from test_projects import SITE, prepare_project_control, project_body
from aiflow.v4 import cli
from aiflow.v4.control.github import GitHubApp
from aiflow.v4.control.jira_connect import JiraAuth, SecretStore
from aiflow.v4.control import api as control_api
from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
from aiflow.v4.control.loops import jira_sync
from aiflow.v4.control.ui import mount_ui
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.install import backup
from aiflow.v4.install import secrets as sec
from aiflow.v4.install.control import Layout

ADMIN = "owner@example.test"
SESSION_SECRET = "acceptance-session-secret-with-32-bytes"
DOC = ROOT / "docs/v4/clean-install-acceptance.md"
TOOLS = {"git": "git version 2.43", "uv": "uv 0.8.1", "python3": "Python 3.12.2",
         "codex": "codex 1.0", "codex-code-mode-host": "present"}


def issue(key, summary, status, labels, issuetype="Story"):
    return {"key": key, "fields": {"summary": summary, "status": {"name": status}, "issuetype": {"name": issuetype},
                                   "priority": {"name": "Medium"}, "labels": list(labels),
                                   "description": "Acceptance criteria:\n- AC1: it works"}}


class Fresh:
    """A clean control machine (install) plus a control API with sign-in on and two clean machines."""

    def __init__(self, machine, ctx, control_repo):
        self.machine, self.ctx, self.control_repo = machine, ctx, control_repo

    def install_control(self, version="v4.1.0", **opts):
        out: list[str] = []
        assert installer(self.machine, out, version=version, admin_email="", **opts).run() == 0, out
        return out

    def start_control_api(self):
        ctx = self.ctx
        env = sec.read_env(self.machine.p("/etc/aiflow/secrets/control/aiflow4.env"))
        assert env["AIFLOW4_CONSOLE_LOGIN"] == "1"  # what the installer wrote is what the API runs with
        ctx.settings.console_login = True
        ctx.settings.console_session_secret = SESSION_SECRET
        ctx.settings.owner_token = env["AIFLOW4_OWNER_TOKEN"]
        prepare_project_control(ctx, self.control_repo)
        self.app = create_app(ctx)
        mount_ui(self.app, ctx)
        self.app.state.sync_accounts()
        self.anonymous = TestClient(self.app, follow_redirects=False, client=("127.0.0.1", 50001))
        self.client = TestClient(self.app, follow_redirects=False, client=("127.0.0.1", 50000))
        assert "Create Administrator" in self.anonymous.get("/login").text
        assert self.anonymous.get("/cc").headers["location"].startswith("/login")
        self.password = "clean-install-browser-password-2026"
        setup = self.client.get("/v1/setup/status")
        assert setup.status_code == 200 and setup.json()["admin_exists"] is False
        created = self.client.post("/v1/setup/bootstrap", json={"public_url": "http://127.0.0.1",
            "email": ADMIN, "password": self.password}, headers={"X-AIFLOW4-Setup": setup.json()["bootstrap_token"]})
        assert created.status_code == 200, created.text
        return self.client

    def sign_in(self):
        r = self.client.post("/v1/auth/login", json={"email": ADMIN, "password": self.password})
        assert r.status_code == 200, r.text
        user = self.client.get("/v1/auth/me").json()
        assert user["role"] == "administrator" and user["role_label"] == "Administrator"

    def join(self, name):
        """Join a machine with the one-use token Command Center issues; the machine itself never signs in."""
        command = self.client.post(f"/v1/nodes/{name}/join-command", json={
            "name": name, "role": "worker", "project": "TESTWP",
        })
        assert command.status_code == 200, command.text
        parts = shlex.split(command.json()["command"])
        assert parts[:2] == ["curl", "-fsSL"] and parts[-2] == "--token"
        token = parts[-1]
        joined = self.client.post("/v1/nodes/join", json={"name": name, "join_token": token})
        assert joined.status_code == 200, joined.text
        headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
        beat = self.client.post(f"/v1/nodes/{name}/heartbeat", json={"tools": TOOLS, "slots_total": 2}, headers=headers)
        assert beat.status_code == 200, beat.text
        return headers, beat.json()

    def machine_client(self):
        """A client without the console cookie: what a machine's own call looks like."""
        return TestClient(self.app, follow_redirects=False, client=("10.44.0.11", 50001))


@pytest.fixture
def fresh(machine, ctx, control_repo, tmp_path, monkeypatch):  # noqa: F811
    releases = tmp_path / "releases"
    release = releases / "v4.1.0"
    release.mkdir(parents=True)
    tarball = b"acceptance signed release fixture"
    filename = "aiflow4-v4.1.0.tar.gz"
    (release / filename).write_bytes(tarball)
    (release / f"{filename}.sigstore.json").write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(tarball).hexdigest()
    (release / "SHA256SUMS").write_text(f"{digest}  {filename}\n", encoding="utf-8")
    (releases / "CURRENT").write_text("v4.1.0\n", encoding="utf-8")
    monkeypatch.setattr(control_api, "RELEASES", releases)
    ctx.settings.join_control_url = "https://control.example.test"
    f = Fresh(machine, ctx, control_repo)
    f.install_control()
    f.start_control_api()
    return f


# --- AC1: clean machines, sign-in on, documented steps only ---------------------------------------------------------
def test_clean_control_machine_asks_for_sign_in_and_first_run_setup_needs_only_the_documented_steps(fresh):
    m, client = fresh.machine, fresh.client
    assert m.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.1.0"
    assert (m.p("/etc/aiflow/secrets/console")).is_dir()
    assert not console_seeds(m)  # the browser created the Administrator; installer did not require an email or file
    # the console and the read APIs refuse anonymous callers; the sign-in page and the join endpoints stay open
    assert fresh.anonymous.get("/v1/state").status_code == 401
    assert fresh.anonymous.get("/cc").status_code == 302 and fresh.anonymous.get("/cc").headers["location"].startswith("/login")
    assert fresh.anonymous.get("/healthz").status_code == 200
    assert fresh.anonymous.post("/v1/auth/login", json={"email": ADMIN, "password": "wrong"}).status_code == 401
    fresh.sign_in()
    assert client.get("/v1/state").status_code == 200

    a, _ = fresh.join("cc")
    b, beat = fresh.join("node1")
    assert beat["desired"]["accounts"]
    nodes = {n["name"]: n for n in client.get("/v1/state").json()["nodes"]}
    assert {"cc", "node1"} <= set(nodes)


def test_every_setup_step_the_test_takes_is_in_the_document():
    text = DOC.read_text()
    for step in ("aiflow4-control-install", "browser setup", "Administrator", "/setup", "Projects",
                 "join command", "/review", "signed candidate", "clean-install-acceptance.sh"):
        assert step in text, step


# --- AC2: add a project, a sample ticket through Intake, development, verification and merge -------------------------
def test_sample_ticket_passes_intake_development_verification_and_merge_and_a_human_ticket_reaches_review(fresh):
    ctx, client = fresh.ctx, fresh.client
    fresh.sign_in()
    cc, _ = fresh.join("cc")
    n1, _ = fresh.join("node1")
    catalog = client.get("/v1/projects/catalog")
    assert catalog.status_code == 200
    assert "NEWAPP" in {project["key"] for project in catalog.json()["jira_projects"]}
    assert "jooservices/new-app" in {repo["full_name"] for repo in catalog.json()["github_repos"]}
    body = project_body("NEWAPP", stack="")
    preflight = client.post("/v1/projects/preflight", json=body)
    assert preflight.status_code == 200 and preflight.json()["ok"] is True, preflight.text
    saved = client.post("/v1/projects", json=body)
    assert saved.status_code == 200, saved.text
    fresh.control_repo.joinpath("accounts.yaml").write_text(
        fresh.control_repo.joinpath("accounts.yaml").read_text()
        + "  - {id: grok-01, provider: grok, node: cc, tier: middle, roles: [intake_refiner]}\n")
    snap = ControlRepo.load(fresh.control_repo)
    ctx.repo = lambda: snap
    ctx.refresh_repo = lambda: None
    fresh.app.state.sync_accounts()

    # Jira (the fake) is the source of truth: the registry only mirrors what the sync reads from it
    jira_now = {"NEWAPP-1": issue("NEWAPP-1", "Sample ticket", "Intake", []),
                "NEWAPP-2": issue("NEWAPP-2", "Needs a human", "To Do", ["aiflow-ready", "require-human"])}
    ctx.jira.status_of.update({"NEWAPP-1": "Intake", "NEWAPP-2": "To Do"})

    def resync():
        for key, row in jira_now.items():
            row["fields"]["status"] = {"name": ctx.jira.status_of[key]}
        ctx.jira.search = lambda _jql: iter(list(jira_now.values()))
        jira_sync(ctx)
        ctx.jira.search = lambda _jql: iter([])

    def move(run, headers, key, to):
        out = client.post(f"/v1/jira/{key}/transition", json={"run_id": run["run_id"], "fence": run["fence"], "to": to},
                          headers=headers)
        assert out.status_code == 200 and out.json() == {"status": to}, out.text
        assert ctx.jira.status_of[key] == to

    resync()
    assert {k: ctx.db.one("SELECT status FROM tickets WHERE key=%s", (k,))["status"] for k in jira_now} == {
        "NEWAPP-1": "Intake", "NEWAPP-2": "To Do"}

    # Intake: the refiner rewrites the description through the fenced endpoint and the ticket becomes ready
    cands = client.get("/v1/work/candidates", params={"account": "grok-01"}, headers=cc).json()["tickets"]
    assert [(t["key"], t["step"]) for t in cands] == [("NEWAPP-1", "intake")]
    r = client.post("/v1/work/claim", json={"ticket": "NEWAPP-1", "account": "grok-01", "step": "intake"}, headers=cc).json()
    rewrite = "Business requirement: x\nAcceptance criteria:\n- AC1: works\nTechnical notes: n\nDefinition of done:\n- tests"
    assert client.post("/v1/jira/NEWAPP-1/description", json={"run_id": r["run_id"], "fence": r["fence"], "body": rewrite},
                       headers=cc).json() == {"ok": True}
    move(r, cc, "NEWAPP-1", "To Do")  # the refiner's fenced Jira transition: Intake -> To Do
    jira_now["NEWAPP-1"]["fields"]["labels"] = ["aiflow-ready"]  # and the label it adds in Jira, which the next sync mirrors
    resync()
    ready = ctx.db.one("SELECT status, labels FROM tickets WHERE key='NEWAPP-1'")
    assert ready["status"] == "To Do" and "aiflow-ready" in ready["labels"]
    client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "done", "summary": "refined"}, headers=cc)
    client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=cc)

    # development on one machine, with the machine's policy call going through the sign-in gate
    dev = client.post("/v1/work/claim", json={"ticket": "NEWAPP-1", "account": "claude-01"}, headers=cc).json()
    assert dev["claimed"], dev
    pr = client.post("/v1/github/pr", json={"run_id": dev["run_id"], "fence": dev["fence"], "repo": "new-app",
                                            "head": "NEWAPP-1-x", "base": "develop", "title": "NEWAPP-1: x"}, headers=cc).json()
    client.post(f"/v1/runs/{dev['run_id']}/result", json={"fence": dev["fence"], "outcome": "done", "pr_url": pr["url"],
                                                          "branch": "NEWAPP-1-x", "summary": "built"}, headers=cc)
    move(dev, cc, "NEWAPP-1", "In Progress")  # development starts, then hands over to verification
    move(dev, cc, "NEWAPP-1", "In Verify")
    client.post(f"/v1/runs/{dev['run_id']}/release", json={"fence": dev["fence"]}, headers=cc)
    resync()
    assert ctx.db.one("SELECT status FROM tickets WHERE key='NEWAPP-1'")["status"] == "In Verify"

    # verification on the other machine, then merge
    v = client.post("/v1/work/claim", json={"ticket": "NEWAPP-1", "account": "codex-01", "step": "verify"}, headers=n1).json()
    assert v["claimed"] and v["pr"]["branch"] == "NEWAPP-1-x"
    merged = client.post("/v1/github/merge", json={"run_id": v["run_id"], "fence": v["fence"], "number": pr["number"],
                                                   "title": "NEWAPP-1: x"}, headers=n1).json()
    assert merged["merged"] and ctx.github.merged[-1][1] == pr["number"]
    move(v, n1, "NEWAPP-1", "Done")  # the verifier closes the merged ticket
    resync()
    assert ctx.db.one("SELECT status, done_at FROM tickets WHERE key='NEWAPP-1'")["done_at"] is not None
    assert [t for t in ctx.jira.transitions if t[0] == "NEWAPP-1"] == [
        ("NEWAPP-1", "To Do"), ("NEWAPP-1", "In Progress"), ("NEWAPP-1", "In Verify"), ("NEWAPP-1", "Done")]

    # the second ticket (label require-human) travels the same workflow: development, a pull request, verification;
    # the verifier hands it to a person through the fenced Jira transition, and the Human review screen lists it
    assert client.get("/v1/reviews").json()["tickets"] == []
    dev2 = client.post("/v1/work/claim", json={"ticket": "NEWAPP-2", "account": "claude-01"}, headers=cc).json()
    assert dev2["claimed"], dev2
    pr2 = client.post("/v1/github/pr", json={"run_id": dev2["run_id"], "fence": dev2["fence"], "repo": "new-app",
                                             "head": "NEWAPP-2-x", "base": "develop", "title": "NEWAPP-2: x"}, headers=cc).json()
    client.post(f"/v1/runs/{dev2['run_id']}/result", json={"fence": dev2["fence"], "outcome": "done", "pr_url": pr2["url"],
                                                           "branch": "NEWAPP-2-x", "summary": "built"}, headers=cc)
    assert ctx.db.one("SELECT status FROM tickets WHERE key='NEWAPP-2'")["status"] == "To Do"  # not moved behind Jira's back
    move(dev2, cc, "NEWAPP-2", "In Progress")
    move(dev2, cc, "NEWAPP-2", "In Verify")
    client.post(f"/v1/runs/{dev2['run_id']}/release", json={"fence": dev2["fence"]}, headers=cc)
    resync()
    assert ctx.db.one("SELECT status FROM tickets WHERE key='NEWAPP-2'")["status"] == "In Verify"
    v2 = client.post("/v1/work/claim", json={"ticket": "NEWAPP-2", "account": "codex-01", "step": "verify"}, headers=n1).json()
    assert v2["claimed"]
    moved = client.post("/v1/jira/NEWAPP-2/transition", json={"run_id": v2["run_id"], "fence": v2["fence"],
                                                              "to": "Human review"}, headers=n1)
    assert moved.status_code == 200 and moved.json() == {"status": "Human review"}
    client.post(f"/v1/runs/{v2['run_id']}/result", json={"fence": v2["fence"], "outcome": "human", "summary": "needs a person"},
                headers=n1)
    client.post(f"/v1/runs/{v2['run_id']}/release", json={"fence": v2["fence"]}, headers=n1)
    assert not [m for m in ctx.github.merged if m[1] == pr2["number"]]  # nothing merged behind the person's back
    review = client.get("/v1/reviews")
    assert review.status_code == 200
    queue = {t["key"]: t for t in review.json()["tickets"]}
    assert list(queue) == ["NEWAPP-2"] and queue["NEWAPP-2"]["status"] == "Human review"
    assert fresh.machine_client().get("/v1/reviews").status_code == 401  # and only for signed-in users


# --- AC3: machines' policy calls with sign-in on ---------------------------------------------------------------------
def test_machines_policy_calls_and_heartbeats_work_with_sign_in_on(fresh):
    ctx = fresh.ctx
    fresh.sign_in()
    cc, _ = fresh.join("cc")
    ctx.db.run("INSERT INTO tickets (key,project,status,labels,summary,description) VALUES"
               " ('TESTWP-7','TESTWP','To Do',%s,'s','d')", (["aiflow-ready"],))
    machine_client = fresh.machine_client()  # no cookie: machines never sign in
    claim = machine_client.post("/v1/work/claim", json={"ticket": "TESTWP-7", "account": "claude-01"}, headers=cc).json()
    token = {"Authorization": f"Bearer {claim['run_token']}"}
    hook = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": "/srv/aiflow/work/x"}
    out = machine_client.post("/v1/policy/decide", json=hook, headers=token)
    assert out.status_code == 200 and out.json()["hookSpecificOutput"]["permissionDecision"] == "deny"
    hook["tool_input"]["command"] = "composer install"
    assert machine_client.post("/v1/policy/decide", json=hook, headers=token).json()[
        "hookSpecificOutput"]["permissionDecision"] == "allow"
    assert machine_client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2}, headers=cc).status_code == 200
    assert machine_client.post("/v1/policy/decide", json=hook).status_code == 401  # no credential at all: refused


# --- AC3: the deploy and canary scripts with sign-in on ---------------------------------------------------------------
class MockControl:
    """A control endpoint with sign-in on: /healthz and /cc are open; deploy registry reads need the owner token."""

    def __init__(self, token):
        self.token, self.requests = token, []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                auth = self.headers.get("Authorization", "")
                mock.requests.append((self.path, auth))
                if self.path == "/v1/state":
                    code, body = 401, b'{"detail":"sign-in required"}'
                elif self.path == "/v1/deploy/state" and auth != f"Bearer {mock.token}":
                    code, body = 401, b'{"detail":"deploy authorization required"}'
                elif self.path == "/v1/deploy/state":
                    code, body = 200, json.dumps({"nodes": [{"name": "node1", "status": "online"}]}).encode()
                else:
                    code, body = 200, b"ok"
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def deploy_state_auth(self):
        return [auth for path, auth in self.requests if path == "/v1/deploy/state"]


def run_promotion(tmp_path, script, env_file, control, previous="v4.1.0", target="v4.2.0"):
    """Run a deploy script on a promotable canary with stubs for docker, the deploy helpers and sleep."""
    root, stubs = tmp_path / "srv", tmp_path / "stubs"
    (root / "control/releases").mkdir(parents=True)
    stubs.mkdir()
    calls = tmp_path / "calls.log"
    (root / "control/releases/CURRENT").write_text(previous)
    (root / "control/releases/CANARY.json").write_text(json.dumps({
        "target": target, "node": "node1", "previous": previous,
        "started_at": "2026-01-01T00:00:00+00:00", "requested_at": "2026-01-01T00:00:00+00:00"}))
    for name, body in {"docker": 'case "$*" in *evaluate*) echo \'{"action":"promote","reason":"stable"}\';; *) echo "{}";; esac',
                       "sleep": "", "aiflow4-deploy": "", "aiflow4-deploy-develop": ""}.items():
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\n{body}\n')
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "AIFLOW4_ROOT": str(root),
           "AIFLOW4_CONTROL_URL": control.url, "AIFLOW4_CONTROL_ENV": str(env_file)}
    done = subprocess.run(["bash", str(ROOT / "deploy/v4/bin" / script)], env=env, capture_output=True, text=True,
                          timeout=120)
    return done, calls.read_text().splitlines() if calls.exists() else []


@pytest.mark.parametrize("script,deploy", [("aiflow4-autodeploy", "aiflow4-deploy"),
                                           ("aiflow4-autodeploy-develop", "aiflow4-deploy-develop")])
def test_deploy_and_canary_scripts_read_the_state_with_the_owner_token_when_sign_in_is_on(fresh, tmp_path, script, deploy):
    env_file = fresh.machine.p("/etc/aiflow/secrets/control/aiflow4.env")  # written by the install
    token = fresh.ctx.settings.owner_token
    control = MockControl(token)
    try:
        done, calls = run_promotion(tmp_path, script, env_file, control)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "deployed" in done.stdout and "rolling back" not in done.stdout, done.stdout
        assert control.deploy_state_auth() and set(control.deploy_state_auth()) == {f"Bearer {token}"}
        assert all(path != "/v1/state" for path, _ in control.requests)  # scripts use the protected deploy route
        assert [c for c in calls if c.startswith(f"{deploy} ")][0].endswith("v4.2.0")
        assert not [c for c in calls if c.endswith("v4.1.0") and c.startswith(deploy)]  # no rollback
        assert token not in done.stdout + done.stderr  # the token is never printed
    finally:
        control.close()


@pytest.mark.parametrize("script,deploy", [("aiflow4-autodeploy", "aiflow4-deploy"),
                                           ("aiflow4-autodeploy-develop", "aiflow4-deploy-develop")])
def test_a_failed_state_health_check_rolls_the_deploy_back(fresh, tmp_path, script, deploy):
    wrong = tmp_path / "wrong.env"
    wrong.write_text("AIFLOW4_OWNER_TOKEN=not-the-owner-token\n")  # state answers 401: the release counts as unhealthy
    control = MockControl(fresh.ctx.settings.owner_token)
    try:
        done, calls = run_promotion(tmp_path, script, wrong, control)
        assert done.returncode == 1, done.stdout + done.stderr
        assert "unhealthy; rolling back to v4.1.0" in done.stdout
        assert [c for c in calls if c.startswith(f"{deploy} ")][-1].endswith("v4.1.0")  # back on the previous release
    finally:
        control.close()


def test_every_deploy_script_parses_and_probes_only_open_endpoints():
    for name in ("aiflow4-deploy", "aiflow4-deploy-develop", "aiflow4-node-install", "aiflow4-control-install",
                 "aiflow4-develop-install", "aiflow4-bootstrap"):
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy/v4/bin" / name)]).returncode == 0, name
        # the deploy health probes use only open endpoints
        assert not re.search(r"curl [^\n]*127\.0\.0\.1/v1/(?!install)", (ROOT / "deploy/v4/bin" / name).read_text()), name


# --- AC3: backup and restore ------------------------------------------------------------------------------------------
def test_ci_backup_and_restore_keep_sign_in_machine_and_integration_credentials_working(
        fresh, test_database_url, tmp_path):
    ctx, lay = fresh.ctx, Layout(fresh.machine.root)
    fresh.sign_in()
    cc, _ = fresh.join("cc")
    ctx.db.run("INSERT INTO tickets (key,project,status,labels,summary,description) VALUES"
               " ('TESTWP-9','TESTWP','To Do',%s,'keep me','d')", (["aiflow-ready"],))
    # Ensure a restored serial value collides with a newly initialized sequence unless restore advances it.
    ctx.db.run("TRUNCATE events RESTART IDENTITY")
    ctx.db.run("INSERT INTO events (type, ticket, outcome, data) VALUES ('backup.test','TESTWP-9','saved','{}')")

    # Seed realistic active Integration credentials on the clean machine. They must be restored along with the
    # control keys because the connected Jira OAuth app and the GitHub App private key live outside `control/`.
    jira_dir = lay.p("/etc/aiflow/secrets/jira")
    github_dir = lay.p("/etc/aiflow/secrets/github")
    jira_store = SecretStore(jira_dir)
    jira_store.save_app("restored-client-id", "restored-client-secret")
    jira_store.save_credential({"kind": "oauth", "cloud_id": "restored-cloud", "site_url": "https://jira.test",
                                "access_token": "expired-access", "refresh_token": "restored-refresh-token",
                                "expires_at": time.time() + 1})
    github_dir.mkdir(parents=True, exist_ok=True)
    github_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    github_pem = github_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                          serialization.NoEncryption())
    (github_dir / "app.json").write_text(json.dumps({"id": "901", "account_login": "acme",
                                                       "installation_id": 77}))
    (github_dir / "app.pem").write_bytes(github_pem)
    (github_dir / "app.json").chmod(0o600)
    (github_dir / "app.pem").chmod(0o600)

    secret_files = [fresh.machine.p("/etc/aiflow/secrets/control/aiflow4.env"),
                    jira_dir / "oauth-app.json", jira_dir / "credential.json",
                    github_dir / "app.json", github_dir / "app.pem"]
    secrets_before = {p: p.read_bytes() for p in secret_files}

    archive = tmp_path / "aiflow-backup.tar.gz"  # invoked only by deploy/v4/ci/acceptance-backup.py
    made = backup.create(test_database_url, archive, lay)
    assert archive.stat().st_mode & 0o777 == 0o600
    assert made["release"] == "v4.1.0" and {"console_users", "nodes", "tickets"} <= set(made["tables"])
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "manifest.json" in names and "db/console_users.copy" in names
    assert "secrets/control/aiflow4.env" in names
    assert {"secrets/jira/oauth-app.json", "secrets/jira/credential.json",
            "secrets/github/app.json", "secrets/github/app.pem"} <= set(names)

    # the disaster: a fresh install has recreated the registry schema and its sequences start at 1.
    with ctx.db.pool.connection() as c:
        c.execute("DROP TABLE " + ", ".join(f'"{t}"' for t in made["tables"]) + " CASCADE")
    ctx.db.migrate()
    shutil.rmtree(lay.p("/etc/aiflow/secrets"))
    assert ctx.db.one("SELECT count(*) AS n FROM console_users")["n"] == 0
    assert fresh.client.post("/v1/auth/login", json={"email": ADMIN, "password": fresh.password}).status_code == 401

    restored = backup.restore(test_database_url, archive, lay)
    assert restored["release"] == "v4.1.0"
    assert {p: p.read_bytes() for p in secret_files} == secrets_before
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in secret_files)
    assert fresh.machine.p("/etc/aiflow/secrets/control").stat().st_mode & 0o777 == 0o700
    assert jira_dir.stat().st_mode & 0o777 == 0o700 and github_dir.stat().st_mode & 0o777 == 0o700
    assert ctx.db.one("SELECT summary FROM tickets WHERE key='TESTWP-9'")["summary"] == "keep me"
    inserted = ctx.db.one(
        "INSERT INTO events (type, ticket, outcome, data) VALUES ('backup.after_restore','TESTWP-9','new','{}') "
        "RETURNING id"
    )
    assert inserted["id"] > 1  # restored id 1 was not reused by the fresh sequence
    again = TestClient(fresh.app, follow_redirects=False, client=("127.0.0.1", 50002))
    assert again.post("/v1/auth/login", json={"email": ADMIN, "password": fresh.password}).status_code == 200
    # the machine resumes with the node token it already had
    assert fresh.machine_client().post("/v1/nodes/cc/heartbeat", json={"slots_total": 2}, headers=cc).status_code == 200

    # Jira can refresh the restored OAuth credential with the restored app secret and refresh token.
    ctx.settings.jira_secret_dir = str(jira_dir)
    jira_requests = []

    def refresh_jira(request):
        payload = json.loads(request.content)
        jira_requests.append(payload)
        assert payload == {"grant_type": "refresh_token", "client_id": "restored-client-id",
                           "client_secret": "restored-client-secret", "refresh_token": "restored-refresh-token"}
        return httpx.Response(200, json={"access_token": "refreshed-jira-access", "expires_in": 3600,
                                         "refresh_token": "rotated-refresh-token"})

    jira_http = httpx.Client(transport=httpx.MockTransport(refresh_jira))
    jira_auth = JiraAuth(ctx.settings, SecretStore(jira_dir), jira_http)
    jira_credential = jira_auth.credential()
    assert jira_requests and jira_credential.headers() == {"Authorization": "Bearer refreshed-jira-access"}
    jira_http.close()

    # The restored GitHub App key and installation id still authorize a fresh installation-token request.
    github_requests = []

    def issue_github_token(request):
        github_requests.append(request)
        assert request.url.path == "/app/installations/77/access_tokens"
        assert request.headers["Authorization"].startswith("Bearer ey")
        return httpx.Response(201, json={"token": "ghs-restored-installation-token",
                                         "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=55)).isoformat()})

    github_http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(issue_github_token))
    github_app = GitHubApp(str(github_dir), "acme", github_http)
    github_token, _ = github_app.token("acme/repo")
    assert github_token == "ghs-restored-installation-token" and github_requests
    github_http.close()


def test_restore_refuses_a_file_that_is_not_a_backup_and_backup_is_not_an_operator_cli(fresh, test_database_url, tmp_path):
    junk = tmp_path / "junk.tar.gz"
    with tarfile.open(junk, "w:gz") as tar:
        info = tarfile.TarInfo("etc/passwd")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(backup.BackupError, match="not an AI Flow backup"):
        backup.restore(test_database_url, junk, Layout(fresh.machine.root))
    for args in (["backup", "create", "--out", str(tmp_path / "unused")], ["backup", "restore", str(junk)]):
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 2


@pytest.mark.parametrize("member", ["secrets/control//etc/passwd", "secrets/control/../x", "secrets/other/f",
                                    "secrets/control/sub/f", "secrets/control/..", "/etc/passwd", "db/unknown.copy",
                                    "db/../x.copy"])
def test_restore_refuses_members_outside_the_allowlist_before_changing_anything(fresh, test_database_url, tmp_path, member):
    lay = Layout(fresh.machine.root)
    good = tmp_path / "good.tar.gz"
    made = backup.create(test_database_url, good, lay)
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(good) as src, tarfile.open(evil, "w:gz") as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m))
        info = tarfile.TarInfo(member)
        info.size = 3
        dst.addfile(info, io.BytesIO(b"bad"))
    users = fresh.ctx.db.one("SELECT count(*) AS n FROM console_users")["n"]
    env = fresh.machine.p("/etc/aiflow/secrets/control/aiflow4.env")
    env_before = env.read_bytes()
    with pytest.raises(backup.BackupError, match="unexpected entry"):
        backup.restore(test_database_url, evil, lay)
    assert fresh.ctx.db.one("SELECT count(*) AS n FROM console_users")["n"] == users and made["tables"]
    assert env.read_bytes() == env_before
    assert not (fresh.machine.root / "etc/passwd").exists()


def test_backup_over_an_existing_world_readable_file_ends_with_mode_0600(fresh, test_database_url, tmp_path):
    out = tmp_path / "old-backup.tar.gz"
    out.write_bytes(b"old")
    out.chmod(0o644)
    backup.create(test_database_url, out, Layout(fresh.machine.root))
    assert out.stat().st_mode & 0o777 == 0o600
    assert tarfile.is_tarfile(out) and not list(tmp_path.glob(".old-backup*"))




# --- AC3: upgrade and rollback ----------------------------------------------------------------------------------------
def test_upgrade_keeps_sign_in_and_data_and_rollback_returns_to_the_previous_release(fresh):
    m = fresh.machine
    before = (m.p("/etc/aiflow/secrets/control/aiflow4.env").read_bytes(),
              m.p("/etc/aiflow/secrets/console").is_dir())
    out: list[str] = []
    assert installer(m, out, version="v4.2.0", admin_email=ADMIN).run() == 0, out
    assert m.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.2.0"
    assert m.p("/etc/aiflow/secrets/control/aiflow4.env").read_bytes() == before[0]  # secrets and sign-in setting kept
    assert sec.read_env(m.p("/etc/aiflow/secrets/control/aiflow4.env"))["AIFLOW4_CONSOLE_LOGIN"] == "1"
    rb: list[str] = []
    assert installer(m, rb, version="", domain="", rollback=True).run() == 0, rb
    assert m.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.1.0"
    # an unhealthy upgrade is undone by itself
    m.broken["v4.3.0"] = {"control-loops"}
    failed: list[str] = []
    assert installer(m, failed, version="v4.3.0", admin_email=ADMIN).run() == 5
    assert m.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.1.0"


# --- AC3: adding a machine and an AI account --------------------------------------------------------------------------
def test_adding_a_machine_and_an_ai_account_while_sign_in_is_on(fresh):
    ctx, client = fresh.ctx, fresh.client
    machine_api = fresh.machine_client()
    assert machine_api.post("/v1/machines/node3/accounts", json={
        "provider": "codex", "sessions": 1, "jobs": ["develop", "verify"],
    }).status_code == 401
    fresh.sign_in()
    headers, _ = fresh.join("node3")  # signed-in Command Center join-command action
    assert any(n["name"] == "node3" for n in client.get("/v1/state").json()["nodes"])

    # This is the Add AI account action used by the Machines screen, with its owner session cookie.
    created = client.post("/v1/machines/node3/accounts", json={
        "provider": "codex", "sessions": 1, "jobs": ["develop", "verify"],
    })
    assert created.status_code == 200, created.text
    account_id = created.json()["account"]
    assert created.json()["node"] == "node3" and created.json()["status"] == "queued"
    assert ctx.db.one("SELECT node FROM accounts WHERE id=%s", (account_id,))["node"] == "node3"

    # The machine retrieves the queued sign-in from its heartbeat, reports provider authorization and a successful
    # account probe, and then receives the account as work it can serve.
    beat = machine_api.post("/v1/nodes/node3/heartbeat", json={"tools": TOOLS, "slots_total": 2}, headers=headers)
    assert beat.status_code == 200, beat.text
    handoff = next(item for item in beat.json()["desired"]["sign_ins"] if item["account"] == account_id)
    assert handoff["provider"] == "codex" and handoff["request_id"]
    assert account_id not in beat.json()["desired"]["accounts"]
    for status, details in (
        ("starting", {}),
        ("waiting", {"sign_in_url": "https://auth.openai.com/codex/device", "code": "ABCD-EFGH"}),
        ("completed", {}),
    ):
        reported = machine_api.post(f"/v1/accounts/{account_id}/sign-in/status",
                                    json={"request_id": handoff["request_id"], "status": status, **details},
                                    headers=headers)
        assert reported.status_code == 200, reported.text
    probed = machine_api.post(f"/v1/accounts/{account_id}/probe", json={"ok": True}, headers=headers)
    assert probed.status_code == 200, probed.text
    beat = machine_api.post("/v1/nodes/node3/heartbeat", json={"tools": TOOLS, "slots_total": 2}, headers=headers)
    assert beat.status_code == 200 and account_id in beat.json()["desired"]["accounts"]
    details = client.get(f"/v1/accounts/{account_id}/sign-in")
    assert details.status_code == 200 and details.json()["code"] == "ABCD-EFGH"
    completed = client.post(f"/v1/accounts/{account_id}/sign-in/complete")
    assert completed.status_code == 200 and completed.json()["signed_in"] is True
    # A machine's own work request is authorized by its node token without a console cookie.
    assert machine_api.get("/v1/work/candidates", params={"account": account_id}, headers=headers).status_code == 200


# --- AC4: CI wiring ---------------------------------------------------------------------------------------------------
def _workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_pull_requests_nightly_and_releases_run_the_acceptance_test_and_failures_open_a_bug(tmp_path):
    ci = _workflow("ci.yml")
    assert "schedule" in ci["on"] and "pull_request" in ci["on"]
    fast, full = ci["jobs"]["acceptance-fast"], ci["jobs"]["acceptance-full"]
    assert "pull_request" in fast["if"] and "schedule" in full["if"]
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "aiflow/v4/(install|control|node)/" in text and "deploy/v4/" in text
    assert "clean-install-acceptance.sh fast" in text and "clean-install-acceptance.sh full" in text
    assert "open-bug-ticket.sh" in text and ci["jobs"]["acceptance-bug"]["needs"] == ["acceptance-full"]

    for job_name, artifact_name in (("acceptance-fast", "clean-install-acceptance-fast"),
                                    ("acceptance-full", "clean-install-acceptance-nightly")):
        upload = next(s for s in ci["jobs"][job_name]["steps"]
                      if s.get("uses", "").startswith("actions/upload-artifact@"))
        assert upload["if"] == "always()"
        assert upload["with"]["name"] == artifact_name
        assert upload["with"]["path"] == "${{ runner.temp }}/aiflow-acceptance-logs/"
    nightly_bug = ci["jobs"]["acceptance-bug"]
    nightly_download = next(s for s in nightly_bug["steps"]
                            if s.get("uses", "").startswith("actions/download-artifact@"))
    assert nightly_download["with"]["name"] == "clean-install-acceptance-nightly"
    assert nightly_download["continue-on-error"] == "true"
    nightly_fallback = next(s for s in nightly_bug["steps"]
                            if "Ensure nightly bug ticket has diagnostic logs" in s.get("name", ""))
    assert nightly_fallback["if"] == "always()" and "artifact-download.log" in nightly_fallback["run"]
    nightly_open = next(s for s in nightly_bug["steps"] if "open-bug-ticket.sh" in s.get("run", ""))
    assert nightly_open["env"]["ACCEPTANCE_LOG_DIR"] == "${{ runner.temp }}/aiflow-acceptance-logs"
    assert "does not automatically open a bug issue" in DOC.read_text()

    release = _workflow("release-v4.yml")
    for index, (workflow, job_name) in enumerate(((ci, "acceptance-full"), (release, "acceptance"))):
        steps = workflow["jobs"][job_name]["steps"]
        release_list = next(s for s in steps if "Two newest published releases" in s.get("name", ""))
        command = release_list["run"]
        assert "set -euo pipefail" in command
        assert 'mkdir -p "$LOGS"' in command and 'QUERY_LOG="$LOGS/release-list.log"' in command
        assert "if TAGS=$(gh release list" in command and '2>>"$QUERY_LOG"' in command
        assert "gh release list failed with exit status" in command and 'exit "$status"' in command
        assert "gh release list" in command and "|| true" not in command
        assert "sort_by(.publishedAt)" in command
        assert 'target=$(printf' in command and 'sed -n 1p' in command
        assert 'previous=$(printf' in command and 'sed -n 2p' in command
        runner_temp = tmp_path / f"release-query-{index}"
        env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(runner_temp),
               "GITHUB_REPOSITORY": "jooservices/ai-flow", "GITHUB_OUTPUT": str(tmp_path / f"output-{index}")}
        failed_query = subprocess.run(
            ["bash", "-c", "gh() { printf 'simulated GitHub API failure\\n' >&2; return 73; }\n" + command],
            env=env, capture_output=True, text=True,
        )
        assert failed_query.returncode == 73
        query_log = runner_temp / "aiflow-acceptance-logs" / "release-list.log"
        assert query_log.is_file()
        assert "failed with exit status 73" in query_log.read_text()
        assert "simulated GitHub API failure" in query_log.read_text()

    gate = release["jobs"]["acceptance"]
    full_step = next(s for s in gate["steps"] if "clean-install-acceptance.sh full" in s.get("run", ""))
    assert "ACCEPTANCE_PREVIOUS" in full_step["env"] and "ACCEPTANCE_TARGET" in full_step["env"]
    assert "${{ steps.releases.outputs.previous }}" in full_step["env"]["ACCEPTANCE_PREVIOUS"]
    assert "${{ steps.releases.outputs.target }}" in full_step["env"]["ACCEPTANCE_TARGET"]
    assert any("Two newest published releases" in s.get("name", "") for s in gate["steps"])
    release_upload = next(s for s in gate["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert release_upload["if"] == "always()"
    assert release_upload["with"]["name"] == "clean-install-acceptance-release"
    assert release_upload["with"]["path"] == "${{ runner.temp }}/aiflow-acceptance-logs/"
    assert set(release["jobs"]["release"]["needs"]) == {"v4-tests", "acceptance"}  # a failure blocks the release
    release_steps = "".join(s.get("run", "") for s in release["jobs"]["release"]["steps"])
    assert "clean-install-containers.sh \"$VERSION\"" in release_steps
    assert "dist/aiflow4-$VERSION.tar.gz.sigstore.json" in release_steps
    assert "dist/SHA256SUMS" in release_steps
    doc = DOC.read_text().lower()
    assert "before it pushes the control image or creates the github release" in doc
    assert ".venv/" in (ROOT / ".gitignore").read_text()
    bug = release["jobs"]["release-bug"]
    assert set(bug["needs"]) == {"v4-tests", "acceptance", "release"} and "failure()" in bug["if"]
    release_download = next(s for s in bug["steps"]
                            if s.get("uses", "").startswith("actions/download-artifact@"))
    assert release_download["with"]["name"] == "clean-install-acceptance-release"
    assert release_download["continue-on-error"] == "true"
    release_fallback = next(s for s in bug["steps"]
                            if "Ensure release bug ticket has diagnostic logs" in s.get("name", ""))
    assert release_fallback["if"] == "always()" and "artifact-download.log" in release_fallback["run"]
    candidate_upload = next(s for s in release["jobs"]["release"]["steps"]
                            if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert candidate_upload["if"] == "always()" and candidate_upload["with"]["name"] == "clean-install-candidate-release"
    candidate_download = next(s for s in bug["steps"] if s.get("with", {}).get("name") == "clean-install-candidate-release")
    assert candidate_download["with"]["path"] == "${{ runner.temp }}/aiflow-acceptance-logs"
    script = (ROOT / "deploy/v4/ci/clean-install-acceptance.sh").read_text()
    assert script.index("clean-install-containers.sh") < script.index("ACCEPTANCE_PREVIOUS:-} && -n")  # one release is enough to install
    release_open = next(s for s in bug["steps"] if "open-bug-ticket.sh" in s.get("run", ""))
    assert release_open["env"]["ACCEPTANCE_LOG_DIR"] == "${{ runner.temp }}/aiflow-acceptance-logs"
    assert (ROOT / "deploy/v4/ci/open-bug-ticket.sh").is_file()
    for script in ("clean-install-acceptance.sh", "open-bug-ticket.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy/v4/ci" / script)]).returncode == 0


def test_real_machine_run_uses_command_center_project_and_machine_setup():
    script = (ROOT / "deploy/v4/ci/clean-install-containers.sh").read_text()
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy/v4/ci/clean-install-containers.sh")]).returncode == 0
    for step in ("--domain", "AIFLOW4_CONSOLE_LOGIN=1", "/v1/setup/bootstrap", "/v1/projects/catalog",
                 "/v1/projects/preflight", "$URL/v1/projects", "/v1/nodes/$1/join-command",
                 "acceptance-backup.py create", "acceptance-backup.py restore"):
        assert step in script, step
    assert "NODES=(" in script and "node-a" in script and "node-b" in script  # a control machine and two machines
    assert "aiflow4-acceptance-provider" in script and "acceptance-providers.py" in script
    assert "/v1/install.sh" in script and "| sh -s -- --token \"$token\"" in script
    assert "AIFLOW4_SERVER_INSTALLER_PREPARED=1" not in script
    assert "pinned server-installer installs and verifies" in script
    assert "aiflow4-node-agent" in script and "/srv/aiflow/current/RELEASE" in script and ":online" in script
    assert "aiflow4-acceptance-cookie.jar" in script
    assert "/v1/nodes/join-tokens" not in script and "AIFLOW4_JOIN_TOKEN" not in script
    assert not re.search(r"cat > .*projects/|printf .*nodes/", script)
    assert "acceptance-providers.py" in script


def test_release_install_helper_matches_the_release_or_candidate_under_test():
    script = (ROOT / "deploy/v4/ci/clean-install-containers.sh").read_text()
    assert "https://raw.githubusercontent.com/jooservices/ai-flow/$VERSION/deploy/v4/bin/aiflow4-control-install" in script
    assert "tar -xOf \"$ARTIFACTS/aiflow4-$VERSION.tar.gz\" deploy/v4/bin/aiflow4-control-install" in script
    assert script.index('"$COSIGN" verify-blob') < script.index('tar -xOf "$ARTIFACTS/aiflow4-$VERSION.tar.gz"')
    assert '--volume "$CONTROL_INSTALLER:/opt/aiflow4-control-install:ro"' in script
    assert '--volume "$ROOT/deploy/v4/bin/aiflow4-control-install:' not in script
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy/v4/ci/clean-install-containers.sh")]).returncode == 0


def test_the_signed_candidate_uses_documented_install_and_join_before_publishing():
    real_machine = (ROOT / "deploy/v4/ci/clean-install-containers.sh").read_text()
    candidate_adapter = (ROOT / "deploy/v4/ci/candidate-deploy.sh").read_text()
    assert "--version \"$VERSION\" --domain \"$DOMAIN\"" in real_machine
    assert "cosign verify-blob" in candidate_adapter
    assert "enable -q --now aiflow4-autodeploy.timer" not in candidate_adapter
    assert "aiflow4-control-install" in real_machine
    assert "/v1/install.sh" in real_machine and "join-command" in real_machine
    assert "AIFLOW4_CONSOLE_LOGIN=1" in real_machine and "/v1/setup/bootstrap" in real_machine and "/v1/auth/login" in real_machine
    assert "AIFLOW4_DATABASE_URL=postgresql://" not in real_machine
    steps = _workflow("release-v4.yml")["jobs"]["release"]["steps"]
    names = [s.get("name", "") for s in steps]
    smoke_step = next(i for i, n in enumerate(names) if n == "Clean-install signed candidate on Ubuntu control and two nodes")
    assert "clean-install-containers.sh" in steps[smoke_step]["run"]
    assert smoke_step < next(i for i, n in enumerate(names) if "GitHub Release" in n or "GHCR" in n)


def test_the_bug_ticket_survives_a_log_longer_than_the_size_limit(tmp_path):
    logs, stubs = tmp_path / "logs", tmp_path / "stubs"
    logs.mkdir()
    stubs.mkdir()
    for n in range(3):  # 3 x 60 lines of 1000 characters: far beyond the 60000 bytes of the body
        (logs / f"step{n}.log").write_text("".join(f"{'x' * 1000}\n" for _ in range(60)))
    gh = stubs / "gh"
    gh.write_text('#!/bin/sh\necho "$@" >> "$GH_CALLS"\n'
                  'while [ $# -gt 0 ]; do [ "$1" = --body-file ] && cp "$2" "$GH_BODY"; shift; done\n')
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "ACCEPTANCE_LOG_DIR": str(logs), "GH_TOKEN": "t",
           "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7", "GH_CALLS": str(tmp_path / "gh.log"),
           "GH_BODY": str(tmp_path / "body.md")}
    done = subprocess.run(["bash", str(ROOT / "deploy/v4/ci/open-bug-ticket.sh"), "nightly failed"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "issue create" in (tmp_path / "gh.log").read_text()
    body = (tmp_path / "body.md").read_text()
    assert len(body) <= 60010 and body.count("```") % 2 == 0 and "actions/runs/7" in body


def test_the_bug_ticket_contains_the_downloaded_acceptance_log_and_requires_it(tmp_path):
    logs, stubs = tmp_path / "logs", tmp_path / "stubs"
    logs.mkdir()
    stubs.mkdir()
    marker = "actual acceptance failure from downloaded artifact"
    (logs / "acceptance-cases.log").write_text(f"{marker}\n")
    gh = stubs / "gh"
    gh.write_text('#!/bin/sh\necho "$@" >> "$GH_CALLS"\n'
                  'while [ $# -gt 0 ]; do [ "$1" = --body-file ] && cp "$2" "$GH_BODY"; shift; done\n')
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "ACCEPTANCE_LOG_DIR": str(logs), "GH_TOKEN": "t",
           "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "7", "GH_CALLS": str(tmp_path / "gh.log"),
           "GH_BODY": str(tmp_path / "body.md")}
    done = subprocess.run(["bash", str(ROOT / "deploy/v4/ci/open-bug-ticket.sh"), "nightly failed"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert marker in (tmp_path / "body.md").read_text()

    (logs / "acceptance-cases.log").unlink()
    failed = subprocess.run(["bash", str(ROOT / "deploy/v4/ci/open-bug-ticket.sh"), "nightly failed"], env=env,
                            capture_output=True, text=True, timeout=60)
    assert failed.returncode != 0
    assert "refusing to open a bug ticket without logs" in failed.stderr
    assert (tmp_path / "gh.log").read_text().count("issue create") == 1


def test_the_topic_is_linked_from_the_deploy_readme():
    assert "(../../docs/v4/clean-install-acceptance.md)" in (ROOT / "deploy/v4/README.md").read_text()


# --- AC5: one case per problem found in real use (add new cases here) -------------------------------------------------
REGRESSIONS = {
    "login gate blocked the machines' own calls": "test_machines_policy_calls_and_heartbeats_work_with_sign_in_on",
    "deploy and canary scripts failed with login enabled": "test_deploy_and_canary_scripts_read_the_state_with_the_owner_token_when_sign_in_is_on",
    "backup omitted active Jira and GitHub App credentials": "test_ci_backup_and_restore_keep_sign_in_machine_and_integration_credentials_working",
    "backup restore had no UI path and must stay CI-only": "test_restore_refuses_a_file_that_is_not_a_backup_and_backup_is_not_an_operator_cli",
    "release acceptance bypassed Command Center project and node setup": "test_real_machine_run_uses_command_center_project_and_machine_setup",
    "the bug ticket was not opened for a long log (broken pipe)": "test_the_bug_ticket_survives_a_log_longer_than_the_size_limit",
    "published clean installs used the checkout's installer instead of the target release":
        "test_release_install_helper_matches_the_release_or_candidate_under_test",
    "release-list API or authentication failures looked like an empty release list":
        "test_pull_requests_nightly_and_releases_run_the_acceptance_test_and_failures_open_a_bug",
    "fresh node bootstrap skipped pinned server-installer preparation":
        "test_real_machine_run_uses_command_center_project_and_machine_setup",
    "machine and AI account acceptance bypassed the Command Center actions":
        "test_adding_a_machine_and_an_ai_account_while_sign_in_is_on",
}


def test_every_known_problem_has_a_case_and_the_document_lists_it():
    names = {n for n in globals() if n.startswith("test_")}
    doc = DOC.read_text()
    for problem, case in REGRESSIONS.items():
        assert case in names, problem
        assert case in doc, problem
