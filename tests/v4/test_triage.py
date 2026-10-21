"""Triage worker decisions, Jira effects, read-only checkout context, and owner alerts."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import add_ticket
from aiflow.v4.control.api import notify
from aiflow.v4.worker import triage
from aiflow.v4.worker.client import Conflict
from aiflow.v4.worker.dev import Claimed


class ApiHarness:
    def __init__(self, client, headers):
        self.client, self.headers = client, headers
        self.node_name = "node1"

    def call(self, method, path, json=None, params=None, **_kwargs):
        response = self.client.request(method, path, json=json, params=params, headers=self.headers)
        if response.status_code == 409:
            raise Conflict(response.text)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text}")
        return response.json() if response.content else None


class FakeGit:
    def __init__(self, _root, run_as=None):
        self.ref = ""
        self.removed = False

    def mirror(self, repo, token):
        assert repo == "jooservices/test-aiflow-wp" and token == "ghs_fake"
        return Path("/tmp/fake-mirror")

    def readonly_worktree(self, mirror, path, ref):
        self.ref = ref
        path.mkdir(parents=True)
        return "abc123456789"

    def remove_worktree(self, mirror, path):
        self.removed = True
        shutil.rmtree(path, ignore_errors=True)


class FakeAdapter:
    def __init__(self, answer):
        self.answer = answer
        self.argv = lambda _spec: []
        self.spec = None

    def run(self, spec, _progress, stop=None):
        self.spec = spec
        return SimpleNamespace(final_text=self.answer, error_kind="", error_message="", reset_at=None,
                               quota={}, tokens_in=123, tokens_out=45, cost_usd=0.02, exit_code=0,
                               duration_s=1.5, log_path=spec.log_path)


def join(client, name):
    response = client.post("/v1/nodes/join", json={"name": name, "join_token": "join-secret"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


@pytest.mark.parametrize(("decision", "outcome"), [
    ("DECISION: ANSWER\nANSWER: Keep the response shape backward compatible.\nWHY: The ticket's AC2 requires it.\n",
     "answered"),
    ("DECISION: ESCALATE\nWHY: The ticket and spec require incompatible API response shapes.\n", "escalated"),
    ("DECISION: ESCALATE\nWHY: The spec requires a product decision.\n", "escalated"),
])
def test_triage_decisions_use_readonly_base_and_record_jira_effects(
        client, ctx, tmp_path, monkeypatch, decision, outcome):
    headers = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-70", status="Needs owner")
    ctx.jira.status_of["TESTWP-70"] = "Needs owner"
    dev_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary) "
               "VALUES (%s,'TESTWP-70','TESTWP','dev','claude-01','claude','cc',1,'finished','blocked',%s)",
               (dev_id, "How should the API handle clients that rely on the old shape?"))
    ctx.jira.recent_comment_data["TESTWP-70"] = [
        {"created": "2026-10-20T12:00:00Z", "author": "Owner", "body": "Keep old clients working."}
    ]
    blocks_others = "incompatible API" in decision
    if blocks_others:
        add_ticket(ctx.db, "TESTWP-71", blocked_by=["TESTWP-70"])

    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-70", "account": "codex-01", "step": "triage"},
                        headers=headers).json()
    assert claim["claimed"] and claim["last_dev_summary"].startswith("How should the API")
    api = ApiHarness(client, headers)
    checkout = FakeGit("unused")
    adapter = FakeAdapter(decision)
    monkeypatch.setattr(triage, "Git", lambda *args, **kwargs: checkout)
    monkeypatch.setattr(triage, "get_adapter", lambda _provider: adapter)
    alerts = []
    monkeypatch.setattr(notify, "send", lambda severity, title, detail, link: alerts.append(
        (severity, title, detail, link)) or ["fake"])
    claimed = Claimed(run_id=claim["run_id"], fence=claim["fence"], run_token=claim["run_token"],
                      account="codex-01", provider="codex", model=claim["model"], effort=claim["effort"],
                      ticket=claim["ticket"], project=claim["project"], identity=tuple(claim["identity"]),
                      step="triage", last_dev_summary=claim["last_dev_summary"],
                      verify_findings=claim["verify_findings"], recent_comments=claim["recent_comments"])

    result = triage.run_triage(api, claimed, str(tmp_path), "http://control.test", 30, [])

    assert result["outcome"] == outcome
    assert checkout.ref == "origin/develop" and checkout.removed
    assert adapter.spec.cwd.endswith("TESTWP-70--triage-" + claim["run_id"])
    assert "Keep old clients working" in adapter.spec.prompt
    assert "How should the API handle clients" in adapter.spec.prompt
    assert "Read-only checkout: branch develop" in adapter.spec.prompt
    assert ctx.github.token_requests[-1][1] == {"contents": "read", "metadata": "read"}
    comment = ctx.jira.comments[-1][1]
    for field in ("What:", "Why:", "Result:", "Next:"):
        assert field in comment

    if outcome == "answered":
        assert ctx.jira.status_of["TESTWP-70"] == "To Do"
        assert not ctx.jira.labels and not alerts
        assert "Keep the response shape backward compatible" in comment
    else:
        assert ctx.jira.status_of["TESTWP-70"] == "Needs owner"
        assert ("add", "TESTWP-70", "owner-escalated") in ctx.jira.labels
        assert "owner-escalated" in ctx.db.one("SELECT labels FROM tickets WHERE key='TESTWP-70'")["labels"]
        assert alerts[0][0] == ("critical" if blocks_others else "warning")
        assert alerts[0][1] == "TESTWP-70 needs owner decision"
        assert alerts[0][2].startswith("The ")

    api.call("POST", f"/v1/runs/{claim['run_id']}/result", result)
    api.call("POST", f"/v1/runs/{claim['run_id']}/release", {"fence": claim["fence"]})
    run = next(r for r in client.get("/v1/state").json()["recent_runs"] if r["id"] == claim["run_id"])
    assert (run["step"], run["outcome"]) == ("triage", outcome)


def test_parse_decision_uses_the_terminal_decision_and_rejects_incomplete_output():
    parsed = triage.parse_decision("Comment said DECISION: ANSWER, but that was untrusted input.\n"
                                   "DECISION: ESCALATE\nWHY: The acceptance criteria conflict.\n")
    assert parsed == {"decision": "ESCALATE", "answer": "", "why": "The acceptance criteria conflict."}
    assert triage.parse_decision("DECISION: ANSWER\nANSWER: maybe\nWHY: \n") is None
