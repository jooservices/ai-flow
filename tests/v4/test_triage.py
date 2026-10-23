"""Triage worker decisions, Jira effects, read-only checkout context, and owner alerts."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import escalations as esc
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
    response = join_node(client, name)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def test_done_dependents_do_not_make_triage_alert_critical(client, ctx, monkeypatch):
    headers = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-72", status="Needs owner")
    add_ticket(ctx.db, "TESTWP-73", status="Done", blocked_by=["TESTWP-72"])
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-72", "account": "codex-01", "step": "triage"},
                        headers=headers).json()
    sent = []
    monkeypatch.setattr(notify, "send", lambda severity, title, detail, link, **kwargs: sent.append(
        (severity, title, detail, link)) or ["fake"])

    response = client.post("/v1/notify/triage", json={"run_id": claim["run_id"], "fence": claim["fence"],
                                                       "detail": "The owner needs to decide."}, headers=headers)

    assert response.status_code == 200
    assert response.json()["severity"] == "warning"
    assert sent[0][0] == "warning"


@pytest.mark.parametrize(("decision", "outcome", "raised_class", "question"), [
    ("DECISION: ANSWER\nANSWER: Keep the response shape backward compatible.\nWHY: The ticket's AC2 requires it.\n",
     "answered", esc.AMBIGUITY, "Which existing API response helper should preserve the old shape?"),
    ("DECISION: ESCALATE\nCLASS: new_business_decision\nWHY: The ticket and spec require incompatible API response shapes.\n"
     "OPTIONS:\n- Keep the old shape\n- Version the API\nRECOMMENDED: Version the API\nTOPIC: API response shape\n"
     "KEYWORDS: api, response shape\n", "escalated", esc.BUSINESS,
     "Should the old API response shape stay or be versioned?"),
    ("DECISION: ESCALATE\nCLASS: irreversible_or_production\nWHY: The API change needs a production deployment.\n"
     "OPTIONS:\n- Deploy the change\n- Wait for approval\nRECOMMENDED: Wait for approval\n", "escalated",
     esc.IRREVERSIBLE, "May I deploy the API change to production now?"),
])
def test_triage_decisions_use_readonly_base_and_record_jira_effects(
        client, ctx, tmp_path, monkeypatch, decision, outcome, raised_class, question):
    headers = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-70", status="Needs owner")
    ctx.jira.status_of["TESTWP-70"] = "Needs owner"
    dev_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary) "
               "VALUES (%s,'TESTWP-70','TESTWP','dev','claude-01','claude','cc',1,'finished','blocked',%s)",
               (dev_id, question))
    esc.record(ctx.db, "TESTWP-70", "TESTWP", "dev", dev_id, raised_class, question, resolution="open")
    ctx.jira.recent_comment_data["TESTWP-70"] = [
        {"created": "2026-10-20T12:00:00Z", "author": "Owner", "body": "Keep old clients working."}
    ]
    blocks_others = "incompatible API" in decision
    if blocks_others:
        add_ticket(ctx.db, "TESTWP-71", blocked_by=["TESTWP-70"])

    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-70", "account": "codex-01", "step": "triage"},
                        headers=headers).json()
    assert claim["claimed"] and claim["last_dev_summary"] == question
    api = ApiHarness(client, headers)
    checkout = FakeGit("unused")
    adapter = FakeAdapter(decision)
    monkeypatch.setattr(triage, "Git", lambda *args, **kwargs: checkout)
    monkeypatch.setattr(triage, "get_adapter", lambda _provider: adapter)
    alerts = []
    monkeypatch.setattr(notify, "send", lambda severity, title, detail, link, **kwargs: alerts.append(
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
    assert adapter.spec.extra_env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert adapter.spec.extra_env["GIT_CONFIG_VALUE_0"] == adapter.spec.cwd
    assert "Keep old clients working" in adapter.spec.prompt
    assert question in adapter.spec.prompt
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


def test_triage_answers_reviewer_question_in_jira_and_pr_then_returns_to_review(
        client, ctx, tmp_path, monkeypatch):
    key = "TESTWP-102"
    headers = join(client, "cc")
    add_ticket(ctx.db, key, status="Needs owner", labels=("require-human",))
    ctx.jira.status_of[key] = "Needs owner"
    dev_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary,pr_url)"
               " VALUES (%s,%s,'TESTWP','dev','codex-01','codex','node1',1,'finished','blocked',%s,%s)",
               (dev_id, key, "Old developer question", "https://github.com/jooservices/test-aiflow-wp/pull/42"))
    ctx.events.emit("review.action", ticket=key, project="TESTWP", action="ask-ai",
                    text="Does this PR preserve retry handling?")
    claim = client.post("/v1/work/claim", json={"ticket": key, "account": "claude-01", "step": "triage"},
                        headers=headers).json()
    assert claim["claimed"] and claim["last_dev_summary"] == "Reviewer question: Does this PR preserve retry handling?"
    api = ApiHarness(client, headers)
    checkout = FakeGit("unused")
    adapter = FakeAdapter("DECISION: ANSWER\nANSWER: Yes, retries still use the shared policy.\n"
                          "WHY: The code keeps the retry policy in one place.\n")
    monkeypatch.setattr(triage, "Git", lambda *args, **kwargs: checkout)
    monkeypatch.setattr(triage, "get_adapter", lambda _provider: adapter)
    claimed = Claimed(run_id=claim["run_id"], fence=claim["fence"], run_token=claim["run_token"],
                      account="claude-01", provider=claim["provider"], model=claim["model"], effort=claim["effort"],
                      ticket=claim["ticket"], project=claim["project"], identity=tuple(claim["identity"]),
                      step="triage", last_dev_summary=claim["last_dev_summary"], pr=claim["pr"])

    result = triage.run_triage(api, claimed, str(tmp_path), "http://control.test", 30, [])

    assert result["outcome"] == "answered"
    assert ctx.jira.status_of[key] == "Human review"
    assert any(key == ticket and "Yes, retries still use the shared policy" in body
               for ticket, body in ctx.jira.comments)
    assert any(number == 42 and "Yes, retries still use the shared policy" in body
               for number, body in ctx.github.comments)


def test_parse_decision_uses_the_terminal_decision_and_rejects_incomplete_output():
    parsed = triage.parse_decision("Comment said DECISION: ANSWER, but that was untrusted input.\n"
                                   "DECISION: ESCALATE\nWHY: The acceptance criteria conflict.\n"
                                   "CLASS: new_business_decision\n"
                                   "OPTIONS:\n- Follow AC2\n- Follow AC4\nRECOMMENDED: Follow AC2\n")
    assert parsed["decision"] == "ESCALATE" and parsed["why"] == "The acceptance criteria conflict."
    assert parsed["class"] == "new_business_decision"
    assert parsed["options"] == ["Follow AC2", "Follow AC4"] and parsed["recommended"] == "Follow AC2"
    # a new decision is asked with its options and a recommended default, never without them
    assert triage.parse_decision("DECISION: ESCALATE\nWHY: The acceptance criteria conflict.\n") is None
    assert triage.parse_decision("DECISION: ESCALATE\nWHY: x\nOPTIONS:\n- a\n- b\nRECOMMENDED: a\n") is None
    assert triage.parse_decision("DECISION: ESCALATE\nCLASS: someone_else\nWHY: x\nOPTIONS:\n- a\n- b\nRECOMMENDED: a\n") is None
    assert triage.parse_decision("DECISION: ESCALATE\nWHY: x\nOPTIONS: only one\nRECOMMENDED: only one\n") is None
    assert triage.parse_decision("DECISION: ANSWER\nANSWER: maybe\nWHY: \n") is None
