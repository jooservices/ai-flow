"""Triage worker decisions, Jira effects, read-only checkout context, and owner alerts."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import add_open_escalation, add_ticket, join_node
from aiflow.v4.control import escalations as esc
from aiflow.v4.control import owner_notifications
from aiflow.v4.worker import triage
from aiflow.v4.worker.client import Conflict
from aiflow.v4.worker.dev import Claimed


class ApiHarness:
    def __init__(self, client, headers):
        self.client, self.headers = client, headers
        self.node_name = "node1"
        self.notification_result = None

    def call(self, method, path, json=None, params=None, **_kwargs):
        response = self.client.request(method, path, json=json, params=params, headers=self.headers)
        if response.status_code == 409:
            self.last_conflict = f"{method} {path} -> 409: {response.text}"
            raise Conflict(self.last_conflict)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text}")
        result = response.json() if response.content else None
        if path == "/v1/notify/triage":
            self.notification_result = result
        return result


class FailingGithubCommentApi(ApiHarness):
    def __init__(self, client, headers):
        super().__init__(client, headers)
        self.github_comment_attempts = 0

    def call(self, method, path, json=None, params=None, **kwargs):
        if path == "/v1/github/comment":
            self.github_comment_attempts += 1
            raise RuntimeError("GitHub is unavailable")
        return super().call(method, path, json=json, params=params, **kwargs)


class FailOnceApi(ApiHarness):
    def __init__(self, client, headers, path):
        super().__init__(client, headers)
        self.path = path
        self.failed = False

    def call(self, method, path, json=None, params=None, **kwargs):
        if path == self.path and not self.failed:
            self.failed = True
            raise RuntimeError("simulated request failure")
        return super().call(method, path, json=json, params=params, **kwargs)


class FakeGit:
    def __init__(self, _root, run_as=None):
        self.ref = ""
        self.removed = False

    def mirror(self, repo, token, _transport_url=""):
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


def test_triage_alert_sends_nothing_without_an_optional_channel(client, ctx):
    headers = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-72", status="Needs owner")
    add_open_escalation(ctx.db, "TESTWP-72")
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-72", "account": "codex-01", "step": "triage"},
                        headers=headers).json()
    question = "Should the response include a status field?"
    asked = esc.ask(ctx, "TESTWP-72", "TESTWP", question, "The API behavior needs a product choice.",
                    ["Include it", "Omit it"], "Include it", "Response status", ["response", "status"])
    esc.settle(ctx.db, "TESTWP-72", "TESTWP", "triage", claim["run_id"], esc.BUSINESS, question,
               "product choice", "person", asked["decision"]["id"], reached_person=True)

    response = client.post("/v1/notify/triage", json={"run_id": claim["run_id"], "fence": claim["fence"],
                                                       "decision_id": asked["decision"]["id"]}, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "unconfigured", "sent": False}
    assert ctx.db.one("SELECT count(*) AS n FROM owner_escalation_notices")["n"] == 0


def _run_triage_decision_case(client, ctx, tmp_path, monkeypatch, decision, raised_class, question,
                             github_comment_fails, insecure_smtp=False):
    headers = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-70", status="Needs owner")
    add_open_escalation(ctx.db, "TESTWP-70")
    ctx.jira.status_of["TESTWP-70"] = "Needs owner"
    dev_id = str(uuid.uuid4())
    dev_summary = "The developer needs a product decision about the response shape."
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary) "
               "VALUES (%s,'TESTWP-70','TESTWP','dev','claude-01','claude','cc',1,'finished','blocked',%s)",
               (dev_id, dev_summary))
    if github_comment_fails:
        ctx.db.run("UPDATE runs SET pr_url=%s WHERE id=%s",
                   ("https://github.com/jooservices/test-aiflow-wp/pull/42", dev_id))
    esc.record(ctx.db, "TESTWP-70", "TESTWP", "dev", dev_id, raised_class, question, resolution="open")
    ctx.jira.recent_comment_data["TESTWP-70"] = [
        {"created": "2026-10-20T12:00:00Z", "author": "Owner", "body": "Keep old clients working."}
    ]
    if "incompatible API" in decision:
        add_ticket(ctx.db, "TESTWP-71", blocked_by=["TESTWP-70"])

    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-70", "account": "codex-01", "step": "triage"},
                        headers=headers).json()
    api = FailingGithubCommentApi(client, headers) if github_comment_fails else ApiHarness(client, headers)
    checkout = FakeGit("unused")
    adapter = FakeAdapter(decision)
    monkeypatch.setattr(triage, "Git", lambda *args, **kwargs: checkout)
    monkeypatch.setattr(triage, "get_adapter", lambda _provider: adapter)
    alerts = []

    class CaptureSender:
        def send(self, message):
            alerts.append(message)

    if insecure_smtp:
        ctx.settings.escalation_email = "owner@example.test"
        ctx.settings.escalation_smtp_host = "configured-smtp"
        ctx.settings.escalation_smtp_username = "flow-user"
        ctx.settings.escalation_smtp_password = "smtp-test-secret"
        ctx.settings.escalation_smtp_tls_mode = "none"
    else:
        monkeypatch.setattr(owner_notifications, "sender_for", lambda _settings: CaptureSender())
    claimed = Claimed(run_id=claim["run_id"], fence=claim["fence"], run_token=claim["run_token"],
                      account="codex-01", provider="codex", model=claim["model"], effort=claim["effort"],
                      ticket=claim["ticket"], project=claim["project"], identity=tuple(claim["identity"]),
                      step="triage", last_dev_summary=claim["last_dev_summary"],
                      triage_question=claim["triage_question"], verify_findings=claim["verify_findings"],
                      recent_comments=claim["recent_comments"], pr=claim.get("pr"))
    result = triage.run_triage(api, claimed, str(tmp_path), "http://control.test", 30, [])
    api.call("POST", f"/v1/runs/{claim['run_id']}/result", result)
    api.call("POST", f"/v1/runs/{claim['run_id']}/release", {"fence": claim["fence"]})
    run = next(r for r in client.get("/v1/state").json()["recent_runs"] if r["id"] == claim["run_id"])
    return {"claim": claim, "api": api, "checkout": checkout, "adapter": adapter,
            "alerts": alerts, "result": result, "run": run, "dev_summary": dev_summary}


def _assert_readonly_triage_case(case, question):
    claim, checkout, adapter = case["claim"], case["checkout"], case["adapter"]
    assert claim["claimed"] and claim["triage_question"] == question
    assert claim["last_dev_summary"] == case["dev_summary"]
    assert claim["triage_question"] != case["dev_summary"]
    assert checkout.ref == "origin/develop" and checkout.removed
    assert adapter.spec.cwd.endswith("TESTWP-70--triage-" + claim["run_id"])
    assert adapter.spec.extra_env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert adapter.spec.extra_env["GIT_CONFIG_VALUE_0"] == adapter.spec.cwd
    assert "Keep old clients working" in adapter.spec.prompt and question in adapter.spec.prompt
    assert case["dev_summary"] not in adapter.spec.prompt
    assert "Read-only checkout: branch develop" in adapter.spec.prompt


def _assert_answered_jira_effects(ctx, comment, alerts):
    for field in ("What:", "Why:", "Result:", "Next:"):
        assert field in comment
    assert ctx.jira.status_of["TESTWP-70"] == "To Do"
    assert not ctx.jira.labels and not alerts
    assert "Keep the response shape backward compatible" in comment


def _assert_escalated_jira_effects(ctx, comment, decision, question, alerts):
    assert ctx.jira.status_of["TESTWP-70"] == "Needs owner"
    assert ("add", "TESTWP-70", "owner-escalated") in ctx.jira.labels
    assert ("add", "TESTWP-70", "needs-owner") in ctx.jira.labels
    labels = set(ctx.db.one("SELECT labels FROM tickets WHERE key='TESTWP-70'")["labels"])
    assert {"owner-escalated", "needs-owner"} <= labels
    assert comment.startswith(question)
    parsed = triage.parse_decision(decision)
    expected_options = "\n".join(f"- {option}" for option in parsed["options"])
    assert f"Options:\n{expected_options}" in comment
    assert f"Recommended option: {parsed['recommended']}" in comment
    assert "Answer in a Jira comment, then move the ticket to To Do." in comment
    jira_comment = next(item for item in ctx.jira.recent_comment_data["TESTWP-70"] if item["body"] == comment)
    saved_time = ctx.db.one("SELECT question_comment_at FROM escalations WHERE ticket_key='TESTWP-70'")
    assert saved_time["question_comment_at"].isoformat() == jira_comment["created"]
    assert len(alerts) == 1
    assert alerts[0] == f"{question}\n{ctx.settings.jira_site.rstrip('/')}/browse/TESTWP-70"


@pytest.mark.parametrize(("decision", "outcome", "raised_class", "question", "github_comment_fails"), [
    ("DECISION: ANSWER\nANSWER: Keep the response shape backward compatible.\nWHY: The ticket's AC2 requires it.\n",
     "answered", esc.AMBIGUITY, "Which existing API response helper should preserve the old shape?", False),
    ("DECISION: ESCALATE\nCLASS: new_business_decision\nWHY: The ticket and spec require incompatible API response shapes.\n"
     "OPTIONS:\n- Keep the old shape\n- Version the API\nRECOMMENDED: Version the API\nTOPIC: API response shape\n"
     "KEYWORDS: api, response shape\n", "escalated", esc.BUSINESS,
     "Should the old API response shape stay or be versioned?", False),
    ("DECISION: ESCALATE\nCLASS: irreversible_or_production\nWHY: The API change needs a production deployment.\n"
     "OPTIONS:\n- Deploy the change\n- Wait for approval\nRECOMMENDED: Wait for approval\n", "escalated",
     esc.IRREVERSIBLE, "May I deploy the API change to production now?", False),
    ("DECISION: ESCALATE\nCLASS: new_business_decision\nWHY: The ticket and spec require incompatible API response shapes.\n"
     "OPTIONS:\n- Keep the old shape\n- Version the API\nRECOMMENDED: Version the API\nTOPIC: API response shape\n"
     "KEYWORDS: api, response shape\n", "escalated", esc.BUSINESS,
     "Should the old API response shape stay or be versioned?", True),
])
def test_triage_decisions_use_readonly_base_and_record_jira_effects(
        client, ctx, tmp_path, monkeypatch, decision, outcome, raised_class, question, github_comment_fails):
    case = _run_triage_decision_case(client, ctx, tmp_path, monkeypatch, decision, raised_class, question,
                                    github_comment_fails)
    assert case["result"]["outcome"] == outcome
    if github_comment_fails:
        assert case["api"].github_comment_attempts == 1
    _assert_readonly_triage_case(case, question)
    assert ctx.github.token_requests[-1][1] == {"contents": "read", "metadata": "read"}
    comment = ctx.jira.comments[-1][1]
    if outcome == "answered":
        _assert_answered_jira_effects(ctx, comment, case["alerts"])
    else:
        _assert_escalated_jira_effects(ctx, comment, decision, question, case["alerts"])
    assert (case["run"]["step"], case["run"]["outcome"]) == ("triage", outcome)


def test_insecure_smtp_failure_does_not_interrupt_jira_owner_handoff(client, ctx, tmp_path, monkeypatch):
    question = "Should the old API response shape stay or be versioned?"
    decision = ("DECISION: ESCALATE\nCLASS: new_business_decision\n"
                "WHY: The ticket and spec require incompatible API response shapes.\n"
                "OPTIONS:\n- Keep the old shape\n- Version the API\nRECOMMENDED: Version the API\n"
                "TOPIC: API response shape\nKEYWORDS: api, response shape\n")
    case = _run_triage_decision_case(client, ctx, tmp_path, monkeypatch, decision, esc.BUSINESS, question,
                                    github_comment_fails=False, insecure_smtp=True)

    assert case["result"]["outcome"] == "escalated"
    assert case["api"].notification_result == {"ok": True, "status": "failed", "sent": False}
    assert ctx.jira.status_of["TESTWP-70"] == "Needs owner"
    labels = set(ctx.db.one("SELECT labels FROM tickets WHERE key='TESTWP-70'")["labels"])
    assert {"needs-owner", "owner-escalated"} <= labels
    assert ctx.jira.comments[-1][1].startswith(question)
    assert ctx.db.one("SELECT count(*) AS n FROM owner_escalation_notices")["n"] == 0
    assert "smtp-test-secret" not in str(case["api"].notification_result)


@pytest.mark.parametrize("failed_write", ("question-comment", "notification", "labels"))
def test_failed_owner_handoff_request_stays_open_and_recovers_on_retry(
        client, ctx, tmp_path, monkeypatch, failed_write):
    key = "TESTWP-73"
    headers = join(client, "node1")
    add_ticket(ctx.db, key, status="Needs owner")
    add_open_escalation(ctx.db, key)
    ctx.jira.status_of[key] = "Needs owner"
    question = "Should the default response remain compatible?"
    dev_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary)"
               " VALUES (%s,%s,'TESTWP','dev','claude-01','claude','cc',1,'finished','blocked',%s)",
               (dev_id, key, "The developer needs a product decision about compatibility."))
    esc.record(ctx.db, key, "TESTWP", "dev", dev_id, esc.BUSINESS, question, "product choice")
    decision_text = ("DECISION: ESCALATE\nCLASS: new_business_decision\nWHY: This is a product decision.\n"
                     "OPTIONS:\n- Keep compatibility\n- Change the response\nRECOMMENDED: Keep compatibility\n")
    monkeypatch.setattr(triage, "Git", lambda *args, **kwargs: FakeGit("unused"))
    monkeypatch.setattr(triage, "get_adapter", lambda _provider: FakeAdapter(decision_text))

    def claimed(response):
        return Claimed(run_id=response["run_id"], fence=response["fence"], run_token=response["run_token"],
                       account="codex-01", provider="codex", model=response["model"], effort=response["effort"],
                       ticket=response["ticket"], project=response["project"], identity=tuple(response["identity"]),
                       step="triage", last_dev_summary=response["last_dev_summary"],
                       triage_question=response["triage_question"], verify_findings=response["verify_findings"],
                       recent_comments=response["recent_comments"], pr=response.get("pr"))

    first_claim = client.post("/v1/work/claim", json={"ticket": key, "account": "codex-01", "step": "triage"},
                              headers=headers).json()
    assert first_claim["claimed"]
    failed_path = {
        "question-comment": f"/v1/escalations/{key}/question-comment",
        "notification": "/v1/notify/triage",
        "labels": f"/v1/jira/{key}/fields",
    }[failed_write]
    failing_api = FailOnceApi(client, headers, failed_path)
    first_result = triage.run_triage(failing_api, claimed(first_claim), str(tmp_path), "http://control.test", 30, [])
    assert first_result["outcome"] == "failed" and failing_api.failed
    failing_api.call("POST", f"/v1/runs/{first_claim['run_id']}/result", first_result)
    failing_api.call("POST", f"/v1/runs/{first_claim['run_id']}/release", {"fence": first_claim["fence"]})

    pending = ctx.db.one("SELECT resolution,decision_id FROM escalations WHERE ticket_key=%s ORDER BY id DESC LIMIT 1",
                         (key,))
    assert pending["resolution"] == "open" and pending["decision_id"]
    assert ctx.db.one("SELECT status FROM owner_decisions WHERE id=%s", (pending["decision_id"],))["status"] == "asked"
    premature = client.post(f"/v1/escalations/{key}/complete-handoff", json={
        "run_id": first_claim["run_id"], "fence": first_claim["fence"], "decision_id": pending["decision_id"]},
        headers=headers)
    assert premature.status_code == 409
    assert ctx.jira.status_of[key] == "Needs owner"
    assert "owner-escalated" not in ctx.db.one("SELECT labels FROM tickets WHERE key=%s", (key,))["labels"]
    if failed_write == "notification":
        assert "needs-owner" not in ctx.db.one("SELECT labels FROM tickets WHERE key=%s", (key,))["labels"]
        assert ctx.db.one("SELECT count(*) AS n FROM owner_escalation_notices")["n"] == 0

    ctx.db.run("UPDATE runs SET ended_at=now()-interval '31 minutes' WHERE id=%s", (first_claim["run_id"],))
    retry_claim = client.post("/v1/work/claim", json={"ticket": key, "account": "codex-01", "step": "triage"},
                              headers=headers).json()
    assert retry_claim["claimed"]
    retry_api = ApiHarness(client, headers)
    retry_result = triage.run_triage(retry_api, claimed(retry_claim), str(tmp_path), "http://control.test", 30, [])
    assert retry_result["outcome"] == "escalated", getattr(retry_api, "last_conflict", retry_result.get("summary"))
    retry_api.call("POST", f"/v1/runs/{retry_claim['run_id']}/result", retry_result)
    retry_api.call("POST", f"/v1/runs/{retry_claim['run_id']}/release", {"fence": retry_claim["fence"]})

    final = ctx.db.one("SELECT resolution FROM escalations WHERE ticket_key=%s ORDER BY id DESC LIMIT 1", (key,))
    labels = set(ctx.db.one("SELECT labels FROM tickets WHERE key=%s", (key,))["labels"])
    owner_comments = [body for ticket, body in ctx.jira.comments if ticket == key and body.startswith(question)]
    assert final["resolution"] == "person"
    assert {"needs-owner", "owner-escalated"} <= labels
    assert len(owner_comments) == 1
    assert "This ticket is grouped" not in owner_comments[0]
    assert ctx.jira.status_of[key] == "Needs owner"


def test_triage_answers_reviewer_question_in_jira_and_pr_then_returns_to_review(
        client, ctx, tmp_path, monkeypatch):
    key = "TESTWP-102"
    headers = join(client, "cc")
    add_ticket(ctx.db, key, status="Needs owner", labels=("require-human",))
    add_open_escalation(ctx.db, key, "Does this PR preserve retry handling?")
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
                      step="triage", last_dev_summary=claim["last_dev_summary"],
                      triage_question=claim["triage_question"], pr=claim["pr"])

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
