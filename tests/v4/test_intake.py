"""Intake step: Intake tickets go to intake_refiner accounts, the worker rewrites them into the template through
the fenced description endpoint, or sends questions to the owner."""
from pathlib import Path

import pytest

from conftest import add_ticket
from aiflow.v4.adapters import RunResult
from aiflow.v4.control.api import create_app
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.worker import intake
from aiflow.v4.worker.dev import Claimed

REWRITE = """Business requirement: Visitors can find the portfolio page.
Acceptance criteria:
- AC1: /portfolio lists the projects
Technical notes: WordPress theme template.
Definition of done:
- tests cover AC1"""


@pytest.fixture
def iclient(ctx, control_repo):
    """Control API whose control repo adds intake_refiner accounts on node cc."""
    with (control_repo / "accounts.yaml").open("a") as f:
        f.write("  - {id: grok-01, provider: grok, node: cc, tier: middle, roles: [intake_refiner]}\n"
                "  - {id: grok-02, provider: grok, node: cc, tier: middle, roles: [intake_refiner], explicit_only: true}\n")
    snap = ControlRepo.load(control_repo)
    ctx.repo = lambda: snap
    from fastapi.testclient import TestClient
    app = create_app(ctx)
    app.state.sync_accounts()
    c = TestClient(app)
    token = c.post("/v1/nodes/join", json={"name": "cc", "join_token": "join-secret"}).json()["node_token"]
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def candidates(client, account):
    return [(t["key"], t["step"]) for t in client.get("/v1/work/candidates", params={"account": account}).json()["tickets"]]


def claim(client, key, account="grok-01", step="intake"):
    r = client.post("/v1/work/claim", json={"ticket": key, "account": account, "step": step})
    assert r.status_code == 200, r.text
    return r.json()


# --- AC1: candidates ---------------------------------------------------------------------------------------
def test_intake_tickets_are_offered_to_intake_refiners_only(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-20", status="Intake", labels=())
    add_ticket(ctx.db, "TESTWP-21", status="Intake", labels=("aiflow-hold",))
    add_ticket(ctx.db, "TESTWP-22", status="To Do")

    assert candidates(iclient, "grok-01") == [("TESTWP-20", "intake")]
    assert ("TESTWP-20", "intake") not in candidates(iclient, "claude-01")  # dev/verify roles only
    assert candidates(iclient, "grok-02") == []  # explicit_only without its ai-grok label
    add_ticket(ctx.db, "TESTWP-20", status="Intake", labels=("ai-grok",))
    assert candidates(iclient, "grok-02") == [("TESTWP-20", "intake")]

    r = claim(iclient, "TESTWP-20")
    assert r["step"] == "intake" and r["ticket"]["description"].startswith("Acceptance criteria")
    assert candidates(iclient, "grok-01") == []  # held by the run
    # a dev claim of an Intake ticket is refused
    assert iclient.post("/v1/work/claim", json={"ticket": "TESTWP-20", "account": "claude-01", "step": "dev"}).status_code == 409


def test_failed_intake_waits_before_the_next_try(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-23", status="Intake", labels=())
    r = claim(iclient, "TESTWP-23")
    iclient.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "failed", "summary": "no answer"})
    iclient.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]})
    assert candidates(iclient, "grok-01") == []
    ctx.db.run("UPDATE runs SET started_at = now() - interval '20 minutes', ended_at = now() - interval '11 minutes'")
    assert candidates(iclient, "grok-01") == [("TESTWP-23", "intake")]


# --- AC3/AC4: description endpoint and fencing ---------------------------------------------------------------
def test_description_endpoint_is_fenced_and_for_intake_runs_only(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-24", status="Intake", labels=())
    add_ticket(ctx.db, "TESTWP-25")
    r = claim(iclient, "TESTWP-24")
    body = {"run_id": r["run_id"], "fence": r["fence"], "body": REWRITE}

    assert iclient.post("/v1/jira/TESTWP-25/description", json=body).status_code == 403  # another ticket
    assert iclient.post("/v1/jira/TESTWP-24/description", json={**body, "body": "  "}).status_code == 400
    assert iclient.post("/v1/jira/TESTWP-24/description", json=body).json() == {"ok": True}
    assert ctx.jira.descriptions == [("TESTWP-24", REWRITE)]
    assert ctx.db.one("SELECT description FROM tickets WHERE key='TESTWP-24'")["description"] == REWRITE
    audit = ctx.db.one("SELECT accepted, result FROM write_audit WHERE action='description'")
    assert audit["accepted"] and audit["result"] == "replaced"

    # the lease moves on: the old fence can no longer replace the description, and the refusal is audited
    ctx.leases.r.delete(f"{ctx.leases.p}lease:TESTWP-24")
    assert ctx.leases.claim("TESTWP-24", "other-run", 60_000).fence == r["fence"] + 1
    assert iclient.post("/v1/jira/TESTWP-24/description", json={**body, "body": "late"}).status_code == 409
    assert ctx.jira.descriptions == [("TESTWP-24", REWRITE)]
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='description' AND NOT accepted")["n"] == 1

    # a dev run holds a valid fence but may not rewrite the ticket
    d = claim(iclient, "TESTWP-25", account="claude-01", step="dev")
    assert iclient.post("/v1/jira/TESTWP-25/description",
                        json={"run_id": d["run_id"], "fence": d["fence"], "body": REWRITE}).status_code == 403


def test_intake_run_may_not_edit_files(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-26", status="Intake", labels=())
    r = claim(iclient, "TESTWP-26")

    def decision(tool, **tool_input):
        hook = {"tool_name": tool, "tool_input": tool_input, "cwd": "/srv/aiflow/work/x"}
        out = iclient.post("/v1/policy/decide", json=hook, headers={"Authorization": f"Bearer {r['run_token']}"})
        return out.json()["hookSpecificOutput"]["permissionDecision"]

    assert decision("Write", file_path="/srv/aiflow/work/x/a.txt") == "deny"
    assert decision("Edit", file_path="/srv/aiflow/work/x/a.txt") == "deny"
    # the project's allowlist holds installs and builds; an intake run may not use them
    for command in ("composer install", "npm install", "git checkout -b x", "echo hi > a.txt", "cat a | tee b",
                    "find . -name '*.php' -delete", "find . -exec rm {} ;", "sort -o out.txt in.txt",
                    "git diff --output=patch.txt", "ls $(rm -rf .)", "env rm a.txt", "rm a.txt"):
        assert decision("Bash", command=command) == "deny", command
    for command in ("ls -la", "cat docs/SPEC.md", "grep -rn 'Business requirement' docs | head -5",
                    "git log --oneline -5", "find . -name '*.php'"):
        assert decision("Bash", command=command) == "allow", command
    assert decision("Read", file_path="/srv/aiflow/work/x/docs/SPEC.md") == "allow"
    # a dev run keeps the project's command allowlist
    add_ticket(ctx.db, "TESTWP-30")
    d = claim(iclient, "TESTWP-30", account="claude-01", step="dev")
    hook = {"tool_name": "Bash", "tool_input": {"command": "composer install"}, "cwd": "/srv/aiflow/work/x"}
    out = iclient.post("/v1/policy/decide", json=hook, headers={"Authorization": f"Bearer {d['run_token']}"}).json()
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


# --- AC2/AC3: the worker ------------------------------------------------------------------------------------
class Api:
    """ControlClient over the in-process control API."""
    node_name = "cc"

    def __init__(self, client):
        self.c = client

    def call(self, method, path, json=None, params=None):
        r = self.c.request(method, path, json=json, params=params)
        if r.status_code == 409:
            from aiflow.v4.worker.client import Conflict
            raise Conflict(r.text)
        assert r.status_code < 400, r.text
        return r.json()


class FakeGit:
    spec: str | None = "# Spec\nStack: WordPress. Every AC needs a PHPUnit test."

    def __init__(self, srv_root, run_as=None):
        pass

    def mirror(self, repo, token):
        return Path("/nonexistent/mirror.git")

    def detached(self, mirror, path, ref):
        (path / "docs").mkdir(parents=True, exist_ok=True)
        if self.spec is not None:
            (path / "docs" / "SPEC.md").write_text(self.spec)
        return "abc123def456"


class FakeAdapter:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def argv(self, spec):
        return ["fake"]

    def run(self, spec, on_progress=None, stop=None):
        self.prompts.append(spec.prompt)
        self.extra_env = spec.extra_env
        return RunResult(exit_code=0, final_text=self.answer, tokens_in=10, tokens_out=20)


def run_worker(iclient, ctx, tmp_path, monkeypatch, key, answer, spec=FakeGit.spec):
    monkeypatch.setattr(FakeGit, "spec", spec)
    adapter = FakeAdapter(answer)
    monkeypatch.setattr(intake, "Git", FakeGit)
    monkeypatch.setattr(intake, "get_adapter", lambda provider: adapter)
    r = claim(iclient, key)
    c = Claimed(run_id=r["run_id"], fence=r["fence"], run_token=r["run_token"], account=r["account"],
                provider=r["provider"], model=r["model"], effort=r["effort"], ticket=r["ticket"], project=r["project"],
                identity=tuple(r["identity"]), step="intake")
    res = intake.run_intake(Api(iclient), c, str(tmp_path), "http://control", 3600, [])
    iclient.post(f"/v1/runs/{c.run_id}/result", json=res)
    return res, adapter


def test_rewrite_keeps_the_original_and_queues_the_ticket(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-27", status="Intake", labels=())
    ctx.db.run("UPDATE tickets SET description='make a portfolio page pls' WHERE key='TESTWP-27'")
    answer = f"I rewrote it.\nDESCRIPTION:\n<<<\n{REWRITE}\n>>>\nINTAKE: REWRITE\n"
    res, adapter = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-27", answer)

    assert res["outcome"] == "refined"
    prompt = adapter.prompts[0]
    assert "make a portfolio page pls" in prompt and "Every AC needs a PHPUnit test." in prompt  # AC2
    assert adapter.extra_env == {}  # the project's node-local secrets never reach an intake run
    comments = [t for k, t in ctx.jira.comments if k == "TESTWP-27"]
    assert len(comments) == 1 and "Original description:" in comments[0] and "make a portfolio page pls" in comments[0]
    assert ctx.jira.descriptions == [("TESTWP-27", REWRITE)]
    assert ("add", "TESTWP-27", "aiflow-ready") in ctx.jira.labels
    assert ctx.jira.status_of["TESTWP-27"] == "To Do"
    actions = [a["action"] for a in ctx.db.all("SELECT action FROM write_audit WHERE ticket='TESTWP-27' ORDER BY id")]
    assert actions == ["token", "comment", "description", "fields", "transition"]  # original saved before the rewrite
    assert ctx.db.one("SELECT outcome FROM runs WHERE ticket_key='TESTWP-27'")["outcome"] == "refined"


def test_questions_move_the_ticket_to_needs_owner(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-28", status="Intake", labels=())
    answer = "QUESTIONS:\n- Which pages list the projects?\n- Is a mobile layout needed?\nINTAKE: QUESTIONS\n"
    res, adapter = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-28", answer, spec=None)

    assert res["outcome"] == "questions"
    assert "has no docs/SPEC.md" in adapter.prompts[0]
    comments = [t for k, t in ctx.jira.comments if k == "TESTWP-28"]
    assert len(comments) == 1 and "Which pages list the projects?" in comments[0] and "mobile layout" in comments[0]
    assert ctx.jira.status_of["TESTWP-28"] == "Needs owner"
    assert not getattr(ctx.jira, "descriptions", [])
    assert ("add", "TESTWP-28", "aiflow-ready") not in ctx.jira.labels


def test_unusable_answer_leaves_the_ticket_in_intake(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-29", status="Intake", labels=())
    res, _ = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-29", "Sure, here is a rewrite: ...")
    assert res["outcome"] == "failed"
    assert "TESTWP-29" not in ctx.jira.status_of and not ctx.jira.comments


def test_malformed_rewrite_is_rejected(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-31", status="Intake", labels=())
    answer = "DESCRIPTION:\n<<<\nMake a portfolio page.\nAcceptance criteria:\n- it works\n>>>\nINTAKE: REWRITE\n"
    res, _ = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-31", answer)
    assert res["outcome"] == "failed" and "does not follow the template" in res["summary"]
    assert "Business requirement:" in res["summary"] and "AC1" in res["summary"]
    assert not getattr(ctx.jira, "descriptions", []) and not ctx.jira.comments
    assert "TESTWP-31" not in ctx.jira.status_of and ("add", "TESTWP-31", "aiflow-ready") not in ctx.jira.labels


def test_parse():
    assert intake.parse(f"DESCRIPTION:\n<<<\n{REWRITE}\n>>>\nINTAKE: REWRITE") == ("rewrite", REWRITE)
    assert intake.parse("QUESTIONS:\n- why?\nINTAKE: QUESTIONS") == ("questions", "- why?")
    assert intake.parse("DESCRIPTION:\n<<<\n\n>>>\nINTAKE: REWRITE") == ("", "")
    assert intake.parse("QUESTIONS:\n- why?\nINTAKE: REWRITE") == ("", "")


@pytest.mark.parametrize("broken, lacks", [
    (REWRITE.replace("Technical notes: WordPress theme template.\n", ""), "Technical notes:"),
    (REWRITE.replace("Business requirement:", "Goal:"), "Business requirement:"),
    (REWRITE.replace("- AC1: /portfolio lists the projects", "- /portfolio lists the projects"), "AC1"),
    (REWRITE.replace("Definition of done:\n- tests cover AC1", ""), "Definition of done:"),
    ("Definition of done:\n- x\n" + REWRITE.replace("Definition of done:\n- tests cover AC1", ""), "Definition of done:"),
    ("just some text", "Acceptance criteria:"),
])
def test_parse_rejects_rewrites_that_miss_the_template(broken, lacks):
    kind, why = intake.parse(f"DESCRIPTION:\n<<<\n{broken}\n>>>\nINTAKE: REWRITE")
    assert kind == "invalid" and lacks in why
