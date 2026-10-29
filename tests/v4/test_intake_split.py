"""JAIF-362: intake returns a validate_plan split list instead of one rewrite."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import add_ticket, join_node
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


PLAN = [
    {"summary": "First child ticket here", "description": "AC1: First child is testable."},
    {"summary": "Second child ticket here", "description": "AC1: Second child is testable."},
]


def _split_answer(plan=PLAN) -> str:
    return f"SPLIT:\n<<<\n{json.dumps(plan)}\n>>>\nINTAKE: SPLIT\n"


@pytest.fixture
def iclient(ctx, control_repo):
    with (control_repo / "accounts.yaml").open("a") as f:
        f.write("  - {id: grok-01, provider: grok, node: cc, tier: middle, roles: [intake_refiner]}\n")
    snap = ControlRepo.load(control_repo)
    ctx.repo = lambda: snap
    from fastapi.testclient import TestClient
    app = create_app(ctx)
    app.state.sync_accounts()
    c = TestClient(app)
    token = join_node(c, "cc").json()["node_token"]
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


class Api:
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
    spec = "# Spec\nStack: WordPress. Every AC needs a PHPUnit test."

    def __init__(self, srv_root, run_as=None):
        pass

    def mirror(self, repo, token, _transport_url=""):
        return Path("/nonexistent/mirror.git")

    def detached(self, mirror, path, ref):
        (path / "docs").mkdir(parents=True, exist_ok=True)
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


def run_worker(iclient, ctx, tmp_path, monkeypatch, key, answer):
    adapter = FakeAdapter(answer)
    monkeypatch.setattr(intake, "Git", FakeGit)
    monkeypatch.setattr(intake, "get_adapter", lambda provider: adapter)
    r = iclient.post("/v1/work/claim", json={"ticket": key, "account": "grok-01", "step": "intake"})
    assert r.status_code == 200, r.text
    claimed = r.json()
    c = Claimed(run_id=claimed["run_id"], fence=claimed["fence"], run_token=claimed["run_token"],
                account=claimed["account"], provider=claimed["provider"], model=claimed["model"],
                effort=claimed["effort"], ticket=claimed["ticket"], project=claimed["project"],
                identity=tuple(claimed["identity"]), step="intake",
                recent_comments=claimed.get("recent_comments") or [])
    res = intake.run_intake(Api(iclient), c, str(tmp_path), "http://control", 3600, [])
    iclient.post(f"/v1/runs/{c.run_id}/result", json=res)
    return res, adapter


def test_parse_accepts_a_valid_split_plan():
    kind, plan = intake.parse(_split_answer())
    assert kind == "split"
    assert plan == PLAN


def test_parse_rejects_an_invalid_split_plan():
    kind, why = intake.parse(_split_answer([PLAN[0]]))
    assert kind == "invalid"
    assert "between 2 and 8" in why


def test_parse_rejects_non_json_split_payload():
    kind, why = intake.parse("SPLIT:\n<<<\nnot-json\n>>>\nINTAKE: SPLIT\n")
    assert kind == "invalid" and "not JSON" in why


def test_needs_split_returns_plan_and_result_applies_children(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-40", status="Intake", labels=())
    monkeypatch.setattr(intake, "_ticket_needs_split", lambda ticket: True)
    res, adapter = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-40", _split_answer())

    assert res["outcome"] == "split_plan"
    assert res["details"]["split_plan"] == PLAN
    assert "This ticket needs splitting" in adapter.prompts[0]
    assert [row["key"] for row in ctx.jira.created] == ["TESTWP-900", "TESTWP-901"]
    assert [row["labels"] for row in ctx.jira.created] == [["aiflow-split-testwp-40-1"], ["aiflow-split-testwp-40-2"]]
    assert "aiflow-ready" not in {label for row in ctx.jira.created for label in row["labels"]}
    assert ("remove", "TESTWP-40", "aiflow-ready") in ctx.jira.labels
    assert ("add", "TESTWP-40", "aiflow-split-parent") in ctx.jira.labels
    assert not getattr(ctx.jira, "descriptions", [])
    assert "TESTWP-40" not in ctx.jira.status_of
    assert ("add", "TESTWP-40", "aiflow-ready") not in ctx.jira.labels


def test_invalid_split_plan_does_not_write_jira(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-41", status="Intake", labels=())
    monkeypatch.setattr(intake, "_ticket_needs_split", lambda ticket: True)
    res, _ = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-41", _split_answer([PLAN[0]]))

    assert res["outcome"] == "failed"
    assert "between 2 and 8" in res["summary"]
    assert getattr(ctx.jira, "created", []) == []
    assert not ctx.jira.comments
    assert not getattr(ctx.jira, "descriptions", [])


def test_ticket_that_does_not_need_split_keeps_rewrite_path(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-42", status="Intake", labels=())
    monkeypatch.setattr(intake, "_ticket_needs_split", lambda ticket: False)
    res, adapter = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-42",
                              f"DESCRIPTION:\n<<<\n{REWRITE}\n>>>\nINTAKE: REWRITE\n")

    assert res["outcome"] == "refined"
    assert "does not need splitting" in adapter.prompts[0]
    assert ctx.jira.descriptions == [("TESTWP-42", REWRITE)]
    assert ctx.jira.status_of["TESTWP-42"] == "To Do"
