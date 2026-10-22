"""Clean-install acceptance test (JAIF-248, docs/v4/clean-install-acceptance.md).

The fast tier: a clean control machine and two clean machines are built from the release with console sign-in on,
first-run setup follows only the documented steps (install, sign in as the Super Administrator, add a project, join
machines), and the first tickets travel through Intake, development, verification and merge. A second ticket that
needs a human reaches the Human review screen. The same sign-in gate then has to let the machines' own calls and the
deploy scripts through, and backup and restore, upgrade and rollback, adding a machine and an AI account work.

The control host, Jira and GitHub are the fakes of the other v4 tests; the release-signed systemd tiers (two Ubuntu
containers) are `deploy/v4/ci/install-smoke.sh` and `control-install-smoke.sh`, driven by
`deploy/v4/ci/clean-install-acceptance.sh full`.

Every problem found later in real use gets a case in REGRESSIONS at the end (AC5).
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import join_node
from test_control_install import ROOT, FakeHost, installer, machine  # noqa: F401 - `machine` is a fixture
from test_projects import SITE, prepare_project_control, project_body
from aiflow.v4 import cli
from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
from aiflow.v4.control.loops import jira_sync
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
        assert installer(self.machine, out, version=version, admin_email=ADMIN, **opts).run() == 0, out
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
        self.app.state.sync_accounts()
        self.client = TestClient(self.app, follow_redirects=False, client=("127.0.0.1", 50000))
        self.password = login.seed_user(ctx.db, ADMIN, "super_admin", "Owner")  # `console-user seed` of the install
        assert self.password
        pw_file = self.machine.p("/etc/aiflow/secrets/console/super-admin-password")  # what the seed CLI leaves behind
        pw_file.parent.mkdir(parents=True, exist_ok=True)
        pw_file.write_text(self.password + "\n")
        pw_file.chmod(0o600)
        return self.client

    def sign_in(self):
        r = self.client.post("/v1/auth/login", json={"email": ADMIN, "password": self.password})
        assert r.status_code == 200, r.text
        assert self.client.get("/v1/auth/me").json()["role"] == "super_admin"

    def join(self, name):
        """Join a machine with the one-use token Command Center issues; the machine itself never signs in."""
        joined = join_node(self.client, name)
        assert joined.status_code == 200, joined.text
        headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
        beat = self.client.post(f"/v1/nodes/{name}/heartbeat", json={"tools": TOOLS, "slots_total": 2}, headers=headers)
        assert beat.status_code == 200, beat.text
        return headers, beat.json()

    def machine_client(self):
        """A client without the console cookie: what a machine's own call looks like."""
        return TestClient(self.app, follow_redirects=False, client=("10.44.0.11", 50001))


@pytest.fixture
def fresh(machine, ctx, control_repo):  # noqa: F811
    f = Fresh(machine, ctx, control_repo)
    f.install_control()
    f.start_control_api()
    return f


# --- AC1: clean machines, sign-in on, documented steps only ---------------------------------------------------------
def test_clean_control_machine_asks_for_sign_in_and_first_run_setup_needs_only_the_documented_steps(fresh):
    m, client = fresh.machine, fresh.client
    assert m.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.1.0"
    assert (m.p("/etc/aiflow/secrets/console/super-admin-password")).parent.is_dir()
    # the console and the read APIs refuse anonymous callers; the sign-in page and the join endpoints stay open
    assert client.get("/v1/state").status_code == 401
    assert client.get("/cc").status_code == 302 and client.get("/cc").headers["location"].startswith("/login")
    assert client.get("/healthz").status_code == 200
    assert client.post("/v1/auth/login", json={"email": ADMIN, "password": "wrong"}).status_code == 401
    fresh.sign_in()
    assert client.get("/v1/state").status_code == 200

    a, _ = fresh.join("cc")
    b, beat = fresh.join("node1")
    assert beat["desired"]["accounts"]
    nodes = {n["name"]: n for n in client.get("/v1/state").json()["nodes"]}
    assert {"cc", "node1"} <= set(nodes)


def test_every_setup_step_the_test_takes_is_in_the_document():
    text = DOC.read_text()
    for step in ("aiflow4-control-install", "--admin-email", "super-admin-password", "/login", "Projects",
                 "join command", "/review", "install-smoke.sh", "clean-install-acceptance.sh"):
        assert step in text, step


# --- AC2: add a project, a sample ticket through Intake, development, verification and merge -------------------------
def test_sample_ticket_passes_intake_development_verification_and_merge_and_a_human_ticket_reaches_review(fresh):
    ctx, client = fresh.ctx, fresh.client
    fresh.sign_in()
    cc, _ = fresh.join("cc")
    n1, _ = fresh.join("node1")
    saved = client.post("/v1/projects", json=project_body("NEWAPP", stack=""))
    assert saved.status_code == 200, saved.text
    fresh.control_repo.joinpath("accounts.yaml").write_text(
        fresh.control_repo.joinpath("accounts.yaml").read_text()
        + "  - {id: grok-01, provider: grok, node: cc, tier: middle, roles: [intake_refiner]}\n")
    snap = ControlRepo.load(fresh.control_repo)
    ctx.repo = lambda: snap
    ctx.refresh_repo = lambda: None
    fresh.app.state.sync_accounts()

    ctx.jira.search = lambda _jql: iter([issue("NEWAPP-1", "Sample ticket", "Intake", []),
                                         issue("NEWAPP-2", "Needs a human", "To Do", ["aiflow-ready", "require-human"])])
    assert jira_sync(ctx) == 2
    ctx.jira.search = lambda _jql: iter([])

    # Intake: the refiner rewrites the description through the fenced endpoint and the ticket becomes ready
    cands = client.get("/v1/work/candidates", params={"account": "grok-01"}, headers=cc).json()["tickets"]
    assert [(t["key"], t["step"]) for t in cands] == [("NEWAPP-1", "intake")]
    r = client.post("/v1/work/claim", json={"ticket": "NEWAPP-1", "account": "grok-01", "step": "intake"}, headers=cc).json()
    rewrite = "Business requirement: x\nAcceptance criteria:\n- AC1: works\nTechnical notes: n\nDefinition of done:\n- tests"
    assert client.post("/v1/jira/NEWAPP-1/description", json={"run_id": r["run_id"], "fence": r["fence"], "body": rewrite},
                       headers=cc).json() == {"ok": True}
    ctx.db.run("UPDATE tickets SET status='To Do', labels=%s WHERE key='NEWAPP-1'", (["aiflow-ready"],))
    client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "done", "summary": "refined"}, headers=cc)
    client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=cc)

    # development on one machine, with the machine's policy call going through the sign-in gate
    dev = client.post("/v1/work/claim", json={"ticket": "NEWAPP-1", "account": "claude-01"}, headers=cc).json()
    assert dev["claimed"], dev
    pr = client.post("/v1/github/pr", json={"run_id": dev["run_id"], "fence": dev["fence"], "repo": "new-app",
                                            "head": "NEWAPP-1-x", "base": "develop", "title": "NEWAPP-1: x"}, headers=cc).json()
    client.post(f"/v1/runs/{dev['run_id']}/result", json={"fence": dev["fence"], "outcome": "done", "pr_url": pr["url"],
                                                          "branch": "NEWAPP-1-x", "summary": "built"}, headers=cc)
    client.post(f"/v1/runs/{dev['run_id']}/release", json={"fence": dev["fence"]}, headers=cc)
    ctx.db.run("UPDATE tickets SET status='In Verify' WHERE key='NEWAPP-1'")

    # verification on the other machine, then merge
    v = client.post("/v1/work/claim", json={"ticket": "NEWAPP-1", "account": "codex-01", "step": "verify"}, headers=n1).json()
    assert v["claimed"] and v["pr"]["branch"] == "NEWAPP-1-x"
    merged = client.post("/v1/github/merge", json={"run_id": v["run_id"], "fence": v["fence"], "number": pr["number"],
                                                   "title": "NEWAPP-1: x"}, headers=n1).json()
    assert merged["merged"] and ctx.github.merged[-1][1] == pr["number"]

    # the second ticket (label require-human) travels the same workflow: development, a pull request, verification;
    # the verifier hands it to a person through the fenced Jira transition, and the Human review screen lists it
    assert client.get("/v1/reviews").json()["tickets"] == []
    dev2 = client.post("/v1/work/claim", json={"ticket": "NEWAPP-2", "account": "claude-01"}, headers=cc).json()
    assert dev2["claimed"], dev2
    pr2 = client.post("/v1/github/pr", json={"run_id": dev2["run_id"], "fence": dev2["fence"], "repo": "new-app",
                                             "head": "NEWAPP-2-x", "base": "develop", "title": "NEWAPP-2: x"}, headers=cc).json()
    client.post(f"/v1/runs/{dev2['run_id']}/result", json={"fence": dev2["fence"], "outcome": "done", "pr_url": pr2["url"],
                                                           "branch": "NEWAPP-2-x", "summary": "built"}, headers=cc)
    client.post(f"/v1/runs/{dev2['run_id']}/release", json={"fence": dev2["fence"]}, headers=cc)
    ctx.db.run("UPDATE tickets SET status='In Verify' WHERE key='NEWAPP-2'")  # what the dev outcome moves in Jira
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
    """A control endpoint with sign-in on: /healthz and /cc are open, /v1/state needs the owner token."""

    def __init__(self, token):
        self.token, self.requests = token, []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                auth = self.headers.get("Authorization", "")
                mock.requests.append((self.path, auth))
                if self.path == "/v1/state" and auth != f"Bearer {mock.token}":
                    code, body = 401, b'{"detail":"sign-in required"}'
                elif self.path == "/v1/state":
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

    def state_auth(self):
        return [auth for path, auth in self.requests if path == "/v1/state"]


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
           "AIFLOW4_CONTROL_URL": control.url, "AIFLOW4_CONTROL_ENV": str(env_file),
           "AIFLOW4_ALERT_HOST": "127.0.0.1", "AIFLOW4_ALERT_PORT": "1"}
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
        assert control.state_auth() and set(control.state_auth()) == {f"Bearer {token}"}  # every read carried the token
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
def test_operator_backup_and_restore_bring_back_sign_in_and_machine_operation(fresh, test_database_url, tmp_path):
    ctx, lay = fresh.ctx, Layout(fresh.machine.root)
    fresh.sign_in()
    cc, _ = fresh.join("cc")
    ctx.db.run("INSERT INTO tickets (key,project,status,labels,summary,description) VALUES"
               " ('TESTWP-9','TESTWP','To Do',%s,'keep me','d')", (["aiflow-ready"],))
    # Ensure a restored serial value collides with a newly initialized sequence unless restore advances it.
    ctx.db.run("TRUNCATE events RESTART IDENTITY")
    ctx.db.run("INSERT INTO events (type, ticket, outcome, data) VALUES ('backup.test','TESTWP-9','saved','{}')")
    secret_files = [fresh.machine.p("/etc/aiflow/secrets/control/aiflow4.env"),
                    fresh.machine.p("/etc/aiflow/secrets/console/super-admin-password")]
    secrets_before = {p: p.read_bytes() for p in secret_files}

    archive = tmp_path / "aiflow-backup.tar.gz"  # the supported backup artifact: `aiflow4 backup create --out F`
    made = backup.create(test_database_url, archive, lay)
    assert archive.stat().st_mode & 0o777 == 0o600
    assert made["release"] == "v4.1.0" and {"console_users", "nodes", "tickets"} <= set(made["tables"])
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "manifest.json" in names and "db/console_users.copy" in names
    assert "secrets/console/super-admin-password" in names and "secrets/control/aiflow4.env" in names

    # the disaster: a fresh install has recreated the registry schema and its sequences start at 1.
    with ctx.db.pool.connection() as c:
        c.execute("DROP TABLE " + ", ".join(f'"{t}"' for t in made["tables"]) + " CASCADE")
    ctx.db.migrate()
    for p in secret_files:
        p.unlink()
    assert ctx.db.one("SELECT count(*) AS n FROM console_users")["n"] == 0
    assert fresh.client.post("/v1/auth/login", json={"email": ADMIN, "password": fresh.password}).status_code == 401

    restored = backup.restore(test_database_url, archive, lay)  # `aiflow4 backup restore F`
    assert restored["release"] == "v4.1.0"
    assert {p: p.read_bytes() for p in secret_files} == secrets_before
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in secret_files)
    assert fresh.machine.p("/etc/aiflow/secrets/control").stat().st_mode & 0o777 == 0o700
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


def test_restore_refuses_a_file_that_is_not_a_backup_and_the_backup_command_checks_its_usage(fresh, test_database_url, tmp_path):
    junk = tmp_path / "junk.tar.gz"
    with tarfile.open(junk, "w:gz") as tar:
        info = tarfile.TarInfo("etc/passwd")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(backup.BackupError, match="not an AI Flow backup"):
        backup.restore(test_database_url, junk, Layout(fresh.machine.root))
    assert cli.main(["backup", "create"]) == 2 and cli.main(["backup", "restore"]) == 2  # usage errors, no side effects




# --- AC3: upgrade and rollback ----------------------------------------------------------------------------------------
def test_upgrade_keeps_sign_in_and_data_and_rollback_returns_to_the_previous_release(fresh):
    m = fresh.machine
    before = (m.p("/etc/aiflow/secrets/control/aiflow4.env").read_bytes(),
              m.p("/etc/aiflow/secrets/console/super-admin-password").parent.is_dir())
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
    fresh.sign_in()
    fresh.join("cc")
    repo = fresh.control_repo
    (repo / "nodes" / "node3.yaml").write_text("name: node3\nip: 10.44.0.13\nslots: 1\nprojects: [TESTWP]\n")
    accounts = repo / "accounts.yaml"
    accounts.write_text(accounts.read_text() + "  - {id: codex-03, provider: codex, node: node3, tier: middle,"
                                               " roles: [dev, verify]}\n")
    snap = ControlRepo.load(repo)
    ctx.repo = lambda: snap
    fresh.app.state.sync_accounts()
    headers, beat = fresh.join("node3")
    assert beat["desired"]["accounts"] == ["codex-03"]
    assert ctx.db.one("SELECT node FROM accounts WHERE id='codex-03'")["node"] == "node3"
    assert any(n["name"] == "node3" for n in client.get("/v1/state").json()["nodes"])
    # the new machine's own call passes the gate with its token only
    assert fresh.machine_client().get("/v1/work/candidates", params={"account": "codex-03"}, headers=headers).status_code == 200


# --- AC4: CI wiring ---------------------------------------------------------------------------------------------------
def _workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_pull_requests_nightly_and_releases_run_the_acceptance_test_and_failures_open_a_bug():
    ci = _workflow("ci.yml")
    assert "schedule" in ci["on"] and "pull_request" in ci["on"]
    fast, full = ci["jobs"]["acceptance-fast"], ci["jobs"]["acceptance-full"]
    assert "pull_request" in fast["if"] and "schedule" in full["if"]
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "aiflow/v4/(install|control|node)/" in text and "deploy/v4/" in text
    assert "clean-install-acceptance.sh fast" in text and "clean-install-acceptance.sh full" in text
    assert "open-bug-ticket.sh" in text and ci["jobs"]["acceptance-bug"]["needs"] == ["acceptance-full"]

    release = _workflow("release-v4.yml")
    gate = release["jobs"]["acceptance"]
    assert "clean-install-acceptance.sh full" in "".join(s.get("run", "") for s in gate["steps"])
    assert set(release["jobs"]["release"]["needs"]) == {"v4-tests", "acceptance"}  # a failure blocks the release
    release_steps = "".join(s.get("run", "") for s in release["jobs"]["release"]["steps"])
    assert "install-smoke.sh \"$VERSION\"" in release_steps  # the signed candidate is tested before publication
    doc = DOC.read_text().lower()
    assert "release relies on the signed install smoke" in doc
    bug = release["jobs"]["release-bug"]
    assert set(bug["needs"]) == {"v4-tests", "acceptance", "release"} and "failure()" in bug["if"]
    assert (ROOT / "deploy/v4/ci/open-bug-ticket.sh").is_file()
    for script in ("clean-install-acceptance.sh", "open-bug-ticket.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy/v4/ci" / script)]).returncode == 0


def test_the_real_machine_run_installs_with_sign_in_and_is_part_of_the_full_run():
    script = (ROOT / "deploy/v4/ci/clean-install-containers.sh").read_text()
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy/v4/ci/clean-install-containers.sh")]).returncode == 0
    for step in ("--domain", "--admin-email", "AIFLOW4_CONSOLE_LOGIN=1", "/v1/nodes/join-tokens", "/v1/nodes/join",
                 "/heartbeat", "backup create", "backup restore", "super-admin-password"):
        assert step in script, step
    assert "NODES=(" in script and "node-a" in script and "node-b" in script  # a control machine and two machines
    assert "clean-install-containers.sh" in (ROOT / "deploy/v4/ci/clean-install-acceptance.sh").read_text()


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


def test_the_topic_is_linked_from_the_deploy_readme():
    assert "(../../docs/v4/clean-install-acceptance.md)" in (ROOT / "deploy/v4/README.md").read_text()


# --- AC5: one case per problem found in real use (add new cases here) -------------------------------------------------
REGRESSIONS = {
    "login gate blocked the machines' own calls": "test_machines_policy_calls_and_heartbeats_work_with_sign_in_on",
    "deploy and canary scripts failed with login enabled": "test_deploy_and_canary_scripts_read_the_state_with_the_owner_token_when_sign_in_is_on",
    "the bug ticket was not opened for a long log (broken pipe)": "test_the_bug_ticket_survives_a_log_longer_than_the_size_limit",
}


def test_every_known_problem_has_a_case_and_the_document_lists_it():
    names = {n for n in globals() if n.startswith("test_")}
    doc = DOC.read_text()
    for problem, case in REGRESSIONS.items():
        assert case in names, problem
        assert case in doc, problem
