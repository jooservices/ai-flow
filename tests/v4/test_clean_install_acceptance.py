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

import re
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import join_node
from test_control_install import ROOT, FakeHost, installer, machine  # noqa: F401 - `machine` is a fixture
from test_projects import SITE, prepare_project_control, project_body
from aiflow.v4.control import api as control_api
from aiflow.v4.control import login
from aiflow.v4.control.api import create_app
from aiflow.v4.control.loops import jira_sync
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.install import secrets as sec

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

    # the second ticket needs a human: its verification ends in "human" and the Human review screen lists it
    ctx.db.run("UPDATE tickets SET status='Human review' WHERE key='NEWAPP-2'")
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,pr_url,summary,"
               "details) VALUES (gen_random_uuid(),'NEWAPP-2','NEWAPP','dev','codex-01','codex','node1',1,'finished','done',"
               "'https://github.com/jooservices/new-app/pull/9','built',%s)", (control_api.J({}),))
    review = client.get("/v1/reviews")
    assert review.status_code == 200
    assert [t["key"] for t in review.json()["tickets"]] == ["NEWAPP-2"]
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
def test_deploy_and_canary_scripts_read_the_state_with_the_owner_token_when_sign_in_is_on(fresh):
    client = fresh.client
    token = fresh.ctx.settings.owner_token
    assert client.get("/v1/state").status_code == 401  # what the scripts got before: every health check failed
    assert client.get("/v1/state", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/v1/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
    for name in ("aiflow4-autodeploy", "aiflow4-autodeploy-develop"):
        script = (ROOT / "deploy/v4/bin" / name).read_text()
        reads = [line for line in script.splitlines() if "/v1/state" in line and not line.lstrip().startswith("#")]
        assert reads and all("state_curl" in line for line in reads), name
        assert "AIFLOW4_OWNER_TOKEN" in script and "Authorization: Bearer" in script
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy/v4/bin" / name)]).returncode == 0
    for name in ("aiflow4-deploy", "aiflow4-deploy-develop", "aiflow4-node-install", "aiflow4-control-install",
                 "aiflow4-develop-install", "aiflow4-bootstrap"):
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy/v4/bin" / name)]).returncode == 0, name
        # the deploy health probes use only open endpoints
        assert not re.search(r"curl [^\n]*127\.0\.0\.1/v1/(?!install)", (ROOT / "deploy/v4/bin" / name).read_text()), name


# --- AC3: backup and restore ------------------------------------------------------------------------------------------
def test_backup_and_restore_bring_back_users_projects_and_machines(fresh):
    ctx = fresh.ctx
    fresh.sign_in()
    fresh.join("cc")
    ctx.db.run("INSERT INTO tickets (key,project,status,labels,summary,description) VALUES"
               " ('TESTWP-9','TESTWP','To Do',%s,'keep me','d')", (["aiflow-ready"],))
    tables = [r["tablename"] for r in ctx.db.all(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    assert {"console_users", "nodes", "tickets"} <= set(tables)
    dump = {}
    with ctx.db.pool.connection() as c:  # logical backup: every table as COPY data
        for t in tables:
            with c.cursor().copy(f'COPY "{t}" TO STDOUT') as copy:
                dump[t] = b"".join(bytes(chunk) for chunk in copy)
    counts = {t: ctx.db.one(f'SELECT count(*) AS n FROM "{t}"')["n"] for t in tables}

    with ctx.db.pool.connection() as c:  # the disaster
        c.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
    assert ctx.db.one("SELECT count(*) AS n FROM console_users")["n"] == 0
    assert fresh.client.post("/v1/auth/login", json={"email": ADMIN, "password": fresh.password}).status_code == 401

    with ctx.db.pool.connection() as c:  # restore into the wiped database (the refused sign-in above left an audit row)
        c.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
        for t in tables:
            with c.cursor().copy(f'COPY "{t}" FROM STDIN') as copy:
                copy.write(dump[t])
    assert {t: ctx.db.one(f'SELECT count(*) AS n FROM "{t}"')["n"] for t in tables} == counts
    assert ctx.db.one("SELECT summary FROM tickets WHERE key='TESTWP-9'")["summary"] == "keep me"
    again = TestClient(fresh.app, follow_redirects=False, client=("127.0.0.1", 50002))
    assert again.post("/v1/auth/login", json={"email": ADMIN, "password": fresh.password}).status_code == 200
    # the secrets of the control machine are files the host backup copies; they stay 0600 and out of the output
    secret_dir = fresh.machine.p("/etc/aiflow/secrets/control")
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in secret_dir.glob("*.env"))


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
    bug = release["jobs"]["release-bug"]
    assert set(bug["needs"]) == {"v4-tests", "acceptance", "release"} and "failure()" in bug["if"]
    assert (ROOT / "deploy/v4/ci/open-bug-ticket.sh").is_file()
    for script in ("clean-install-acceptance.sh", "open-bug-ticket.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy/v4/ci" / script)]).returncode == 0


def test_the_topic_is_linked_from_the_deploy_readme():
    assert "(../../docs/v4/clean-install-acceptance.md)" in (ROOT / "deploy/v4/README.md").read_text()


# --- AC5: one case per problem found in real use (add new cases here) -------------------------------------------------
REGRESSIONS = {
    "login gate blocked the machines' own calls": "test_machines_policy_calls_and_heartbeats_work_with_sign_in_on",
    "deploy and canary scripts failed with login enabled": "test_deploy_and_canary_scripts_read_the_state_with_the_owner_token_when_sign_in_is_on",
}


def test_every_known_problem_has_a_case_and_the_document_lists_it():
    names = {n for n in globals() if n.startswith("test_")}
    doc = DOC.read_text()
    for problem, case in REGRESSIONS.items():
        assert case in names, problem
        assert case in doc, problem
