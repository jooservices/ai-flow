"""Only real business decisions reach a person (docs/v4/escalations.md): AC1-AC8 of JAIF-255."""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import escalations as esc
from aiflow.v4.control.api import notify
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.github import GitHubApp
from aiflow.v4.control.jira import Jira
from aiflow.v4.control.policy import decide, decide_read_only
from aiflow.v4.worker import dev, triage
from aiflow.v4.worker.dev import Claimed, build_prompt, fetch_ci_log

from test_triage import ApiHarness, FakeAdapter, FakeGit

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "escalations-2026-10-22-23.json").read_text())
SECRET = "test-console-session-secret-that-is-long-enough"
POLICY = {"commands": {"allow": ["composer *", "npm *", "vendor/bin/*"], "deny": ["git push*", "git commit*", "sudo *"]},
          "files": {"write_within": "worktree", "deny_read": ["/etc/aiflow/secrets/**"]}}
WORKTREE = "/srv/aiflow/work/TESTWP/TESTWP-1-x"


def owner(client, ctx, role="owner"):
    ctx.settings.console_session_secret = SECRET
    client.cookies.set(COOKIE_NAME, issue_session("owner@example.test", role, 4_000_000_000, SECRET))


def node_headers(client, name="node1"):
    response = join_node(client, name)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def run_of(ticket: str, node: str = "node1") -> dict:
    return {"id": str(uuid.uuid4()), "ticket_key": ticket, "project": ticket.split("-")[0], "node": node}


def put_ticket(ctx, key: str, description: str, status: str = "Needs owner") -> None:
    add_ticket(ctx.db, key, status=status)
    ctx.db.run("UPDATE tickets SET description=%s WHERE key=%s", (description, key))


# --- AC1: every escalation is classified when it is raised; only two classes reach a person --------------------
def test_there_are_seven_classes_and_only_two_reach_a_person():
    assert len(esc.CLASSES) == 7
    assert set(esc.PERSON_CLASSES) == {esc.BUSINESS, esc.IRREVERSIBLE}
    assert all(esc.FIXES[c] and esc.LABELS[c] for c in esc.CLASSES)


@pytest.mark.parametrize("item", FIXTURE["escalations"], ids=lambda i: f"{i['ticket']}@{i['at'][5:13]}")
def test_every_recorded_escalation_gets_its_class(ctx, item):
    c = esc.classify(ctx.db, item["text"], item["ticket"].split("-")[0])
    assert c.cls == item["expected_class"], (c.cls, c.reason)
    assert c.reaches_person == item["reaches_person"]


def test_each_of_the_seven_classes_is_covered_by_the_recorded_escalations():
    assert {i["expected_class"] for i in FIXTURE["escalations"]} == set(esc.CLASSES)


@pytest.mark.parametrize("question", [
    "Which HTTP method does `/v1/foo` use?",
    "What does `/v1/work/claim` return?",
    "Which status code does POST `/v1/escalations/raise` return?",
])
def test_repository_answerable_route_question_does_not_reach_a_person(ctx, question):
    put_ticket(ctx, "TESTWP-401", "Acceptance criteria:\n- AC1: document the existing route")
    raised = esc.dev_question(ctx, run_of("TESTWP-401"), question)
    assert raised["class"] == esc.AMBIGUITY and not raised["resolved"]
    answered = esc.triage_decision(ctx, run_of("TESTWP-401"), {
        "decision": "ANSWER", "answer": "GET, as declared by the route in the repository.",
        "why": "The route declaration answers this fact.", "class": esc.AMBIGUITY}, question)
    assert answered == {"action": "answered", "class": esc.AMBIGUITY}
    row = ctx.db.one("SELECT class,resolution,reached_person FROM escalations WHERE ticket_key='TESTWP-401'")
    assert row == {"class": esc.AMBIGUITY, "resolution": "triage", "reached_person": False}


@pytest.mark.parametrize("question", [
    "Where is the deploy config defined?",
    "What value does the existing session timeout use?",
])
def test_repository_supported_deploy_and_existing_setting_questions_are_not_person_facing(ctx, question):
    key = "TESTWP-402"
    put_ticket(ctx, key, "Acceptance criteria:\n- AC1: document the existing configuration")
    classified = esc.classify(ctx.db, question, "TESTWP")
    assert classified.cls == esc.AMBIGUITY
    raised = esc.dev_question(ctx, run_of(key), question)
    assert raised["class"] == esc.AMBIGUITY and not raised["resolved"]
    answered = esc.triage_decision(ctx, run_of(key), {"decision": "ANSWER", "answer": "Found in the repository.",
                                                      "why": "The existing code answers this."}, question)
    assert answered == {"action": "answered", "class": esc.AMBIGUITY}


def test_explicit_production_deploy_is_still_person_facing(ctx):
    assert esc.classify(ctx.db, "Deploy this change to production?", "TESTWP").cls == esc.IRREVERSIBLE


def test_standard_missing_tool_marker_is_classified_as_machine_environment(ctx):
    classified = esc.classify(ctx.db, "MISSING-TOOL: php, composer - phpunit cannot run", "TESTWP")
    assert classified.cls == esc.MISSING and classified.environment == "php"


# --- AC8: replay of 2026-10-22 and 23 ---------------------------------------------------------------------------
def test_replay_of_the_22_and_23_october_escalations_sends_none_of_the_five_families_to_a_person(client, ctx, monkeypatch):
    alerts = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: alerts.append(a) or ["fake"])
    ctx.db.run("INSERT INTO nodes (name, role, status, tools) VALUES ('node1','worker','online',%s)"
               " ON CONFLICT (name) DO UPDATE SET tools=EXCLUDED.tools", (json.dumps(FIXTURE["node_tools"]),))
    reached = []
    results = {}
    for index, item in enumerate(FIXTURE["escalations"]):
        key = item["ticket"]
        put_ticket(ctx, key, item["description"])
        run = run_of(key)
        result = esc.dev_question(ctx, run, item["text"])
        assert result["class"] == item["expected_class"], item
        results[(key, index)] = result
        if result["class"] in esc.PERSON_CLASSES:
            asked = esc.triage_decision(ctx, run, {
                "decision": "ESCALATE", "class": result["class"], "why": item["text"], "options": item["options"],
                "recommended": item["recommended"], "topic": item["topic"], "keywords": item["keywords"]}, item["text"])
            assert asked["action"] == "asked" and asked["options"] and asked["recommended"]
            reached.append(key)
        elif not result["resolved"]:
            # a question the repository can answer goes to the triage AI, which answers it
            assert result["class"] == esc.AMBIGUITY
            answered = esc.triage_decision(ctx, run, {"decision": "ANSWER", "answer": "Use fmt_date in aiflow/v4/core/text.py.",
                                                       "why": "AGENTS.md", "class": ""}, item["text"])
            assert answered["action"] == "answered" and answered["class"] == esc.AMBIGUITY
    rows = ctx.db.all("SELECT * FROM escalations ORDER BY id")
    assert len(rows) == len(FIXTURE["escalations"])
    assert {r["class"] for r in rows} == set(esc.CLASSES)  # every one is classified
    person_rows = [r for r in rows if r["reached_person"]]
    assert sorted(r["ticket_key"] for r in person_rows) == sorted(reached) == ["JOOWP-320", "JOOWP-322"]
    assert all(r["class"] in esc.PERSON_CLASSES for r in person_rows)
    for row in rows:
        if row["class"] not in esc.PERSON_CLASSES:
            assert not row["reached_person"] and row["resolution"] != "open", row
    assert not alerts  # nothing was sent to the owner by the replay itself
    # JOOWP-312 asked three times for Docker; each time the register answered
    assert [r["class"] for r in rows if r["ticket_key"] == "JOOWP-312"] == [esc.TAKEN] * 3
    # what the machine has is used, what it lacks is parked, nothing is asked
    assert results[("JAIF-222", 1)]["outcome"] == "auto_answered" and "installed" in results[("JAIF-222", 1)]["answer"]
    assert results[("JAIF-130", 0)]["outcome"] == "auto_answered" and "/v1/github/ci-log" in results[("JAIF-130", 0)]["answer"]
    unreportable = results[("JOOWP-304", 9)]
    assert unreportable["outcome"] == "failed" and unreportable["missing_tools"] == []
    assert "cannot report dev-vm" in unreportable["answer"]
    assert ctx.db.one("SELECT count(*) AS n FROM tool_waits WHERE ticket_key='JOOWP-304'")["n"] == 0
    # the policy message of JOOWP-308 explains the allowed alternative
    assert "Edit" in results[("JOOWP-308", 3)]["answer"]
    # the weekly numbers count every row
    metrics = esc.weekly_metrics(ctx.db, now=datetime(2026, 10, 24, tzinfo=timezone.utc))
    assert metrics["this_week"]["total"] == len(rows) == len(FIXTURE["escalations"])


# --- AC2: the register is applied, cited, and the same question never goes to a person twice --------------------
def test_register_entry_is_applied_cited_and_counted(ctx):
    put_ticket(ctx, "JOOWP-312", "Acceptance criteria:\n- AC1: x")
    docker = ctx.db.one("SELECT * FROM owner_decisions WHERE topic='no-docker-for-ai-runs'")
    assert docker["status"] == "decided" and docker["cited_count"] == 0
    for text in ("Need Docker for the tests", "docker-compose up fails here", "Can I use podman instead of Docker?"):
        result = esc.dev_question(ctx, run_of("JOOWP-312"), text)
        assert result["class"] == esc.TAKEN and result["resolved"] and result["decision_id"] == docker["id"]
        assert result["citation"].startswith(f'Owner decision D{docker["id"]} "No Docker for AI runs"')
        assert "No." in result["answer"]
    assert ctx.db.one("SELECT cited_count FROM owner_decisions WHERE id=%s", (docker["id"],))["cited_count"] == 3


def test_exact_question_is_deduplicated_after_owner_answer_even_when_keywords_change(ctx):
    question = "Should customer exports default to CSV or JSON?"
    first = esc.ask(ctx, "TESTWP-410", "TESTWP", question, "Choose the export default.",
                    ["CSV", "JSON"], "JSON", "Export format", ["orchid", "bicycle"])
    assert first["action"] == "asked"
    answered = esc.answer_decision(ctx, first["decision"]["id"], "CSV", "Owner chose CSV", "owner")
    assert answered["decision"].startswith("CSV")

    repeated = esc.ask(ctx, "TESTWP-411", "TESTWP", question, "Choose the export default.",
                       ["JSON", "XML"], "JSON", "Billing default", ["quartz", "lighthouse"])
    assert repeated["action"] == "applied"
    assert repeated["decision"]["id"] == first["decision"]["id"]
    assert repeated["decision"]["decision"] == answered["decision"]
    assert repeated["citation"].startswith(f'Owner decision D{first["decision"]["id"]}')
    assert ctx.db.one("SELECT count(*) AS n FROM owner_decisions WHERE question_key=%s",
                      (esc.normalize_question(question),))["n"] == 1


@pytest.mark.parametrize("topic", ["no-release-or-tag-without-owner", "company-name-jooservices", "plans-and-limits",
                                   "quality-thresholds", "no-docker-for-ai-runs"])
def test_seeded_decisions_cover_what_the_owner_already_decided(ctx, topic):
    row = ctx.db.one("SELECT * FROM owner_decisions WHERE topic=%s", (topic,))
    assert row["status"] == "decided" and row["decision"] and row["patterns"] and row["decided_by"] == "owner"


def test_citation_and_next_run_prompt_carry_the_register_answer(client, ctx):
    headers = node_headers(client, "cc")
    add_ticket(ctx.db, "TESTWP-9")
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-9", "account": "claude-01"}, headers=headers).json()
    assert claim["claimed"]
    raised = client.post("/v1/escalations/raise", json={"run_id": claim["run_id"], "fence": claim["fence"],
                                                         "question": "Should I tag the release v2.0.0?"}, headers=headers)
    assert raised.status_code == 200, raised.text
    body = raised.json()
    assert body["class"] == esc.TAKEN and body["resolved"] and body["outcome"] == "auto_answered"
    assert body["citation"].startswith("Owner decision D") and "No release or tag without the owner" in body["citation"]
    client.post(f"/v1/runs/{claim['run_id']}/result", json={"fence": claim["fence"], "outcome": "auto_answered",
                                                             "summary": "Should I tag the release v2.0.0?"}, headers=headers)
    client.post(f"/v1/runs/{claim['run_id']}/release", json={"fence": claim["fence"]}, headers=headers)
    again = client.post("/v1/work/claim", json={"ticket": "TESTWP-9", "account": "claude-01"}, headers=headers).json()
    assert again["claimed"] and "A release or tag is made only by the owner" in again["triage_answer"]
    assert again["triage_question"] == "Should I tag the release v2.0.0?"
    audit = ctx.db.one("SELECT accepted, result FROM write_audit WHERE target='escalation' AND action='raise'")
    assert audit == {"accepted": True, "result": esc.TAKEN}


def test_only_a_developer_run_raises_an_escalation_and_the_fence_is_checked(client, ctx):
    headers = node_headers(client, "cc")
    add_ticket(ctx.db, "TESTWP-9")
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-9", "account": "claude-01"}, headers=headers).json()
    stale = client.post("/v1/escalations/raise", json={"run_id": claim["run_id"], "fence": claim["fence"] + 5,
                                                        "question": "Docker?"}, headers=headers)
    assert stale.status_code == 409


def test_dev_worker_posts_the_citation_and_sends_the_ticket_back_to_work(monkeypatch, tmp_path):
    class Api:
        node_name = "cc"

        def __init__(self):
            self.calls = []

        def call(self, method, path, body=None, params=None):
            self.calls.append((method, path, body))
            if path == "/v1/escalations/raise":
                return {"class": esc.TAKEN, "label": esc.LABELS[esc.TAKEN], "reason": "the owner already decided this",
                        "resolved": True, "outcome": "auto_answered", "answer": "No. Docker is not used for AI runs.",
                        "citation": 'Owner decision D1 "No Docker for AI runs"', "missing_tools": []}
            return {"token": "t", "url": "https://github.com/x/y/pull/1", "number": 1}

    class Git:
        def __init__(self, *_a, **_k):
            pass

        def mirror(self, *_a):
            return "mirror"

        def foreign_worktree(self, *_a):
            return None

        def worktree(self, _m, _p, _b, base):
            return "origin/" + base

        def changed(self, _path):
            return []

    class Adapter:
        argv = lambda self, _spec: []  # noqa: E731

        def run(self, *_a, **_k):
            return SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=0, quota={}, error_kind="", reset_at=None,
                                   verdict="BLOCKED", verdict_reason="Docker is needed for the tests", error_message="",
                                   final_text="", permission_denials=[], exit_code=0, duration_s=1, log_path="")

    monkeypatch.setattr(dev, "Git", Git)
    monkeypatch.setattr(dev, "get_adapter", lambda _p: Adapter())
    project = {"key": "TESTWP", "repo": "jooservices/test-aiflow-wp", "base": "develop", "checks": [],
               "work_minutes": {"hard": 60}, "merge": {"method": "squash"}}
    claimed = Claimed(run_id="run-1", fence=3, run_token="t", account="claude-01", provider="claude", model="opus",
                      effort="", ticket={"key": "TESTWP-1", "project": "TESTWP", "summary": "S", "description": "AC1",
                                         "labels": [], "priority": "Medium", "type": "Story"},
                      project=project, identity=("bot", "bot@example.test"))
    api = Api()
    result = dev.run_dev(api, claimed, str(tmp_path), "https://control", 3600, [])
    assert result["outcome"] == "auto_answered" and result["details"]["escalation_class"] == esc.TAKEN
    comments = [c[2]["body"] for c in api.calls if c[1].endswith("/comment")]
    assert any('Owner decision D1 "No Docker for AI runs"' in body and "No. Docker is not used" in body for body in comments)
    assert [c[2]["to"] for c in api.calls if c[1].endswith("/transition")][-1] == "To Do"
    assert not any("question for the owner" in body for body in comments)


# --- AC3: a new kind of decision is asked once, with options and a recommended default ------------------------------
NEW = {"decision": "ESCALATE", "class": esc.BUSINESS, "why": "Failed payment emails: timing is a product decision.",
       "options": ["Email immediately", "Email after 24 hours"], "recommended": "Email after 24 hours",
       "topic": "Failed payment email timing", "keywords": ["failed payment", "email"]}


def test_a_new_decision_is_asked_once_then_becomes_a_register_entry(client, ctx):
    owner(client, ctx)
    put_ticket(ctx, "JOOWP-322", "Acceptance criteria:\n- AC1: x")
    put_ticket(ctx, "JOOWP-323", "Acceptance criteria:\n- AC1: y")
    ctx.jira.status_of.update({"JOOWP-322": "Needs owner", "JOOWP-323": "Needs owner"})
    question = "Should a failed payment email go out immediately or after 24 hours?"
    esc.dev_question(ctx, run_of("JOOWP-322"), question)
    first = esc.triage_decision(ctx, run_of("JOOWP-322"), NEW, question)
    assert first["action"] == "asked" and first["options"] == NEW["options"] + [] and first["recommended"] == NEW["recommended"]
    pending = ctx.db.one("SELECT * FROM owner_decisions WHERE id=%s", (first["decision_id"],))
    assert pending["status"] == "asked" and pending["options"] == NEW["options"] and pending["decision"] == ""
    # another ticket asks the same thing: it joins the wait, nobody is asked again
    esc.dev_question(ctx, run_of("JOOWP-323"), question)
    alternate = {**NEW, "why": "The email timing needs a product choice.", "topic": "Payment follow-up",
                 "keywords": ["outbound", "message", "schedule"]}
    second = esc.triage_decision(ctx, run_of("JOOWP-323"), alternate, question)
    assert second["action"] == "waiting" and second["decision_id"] == first["decision_id"]
    waiting = ctx.db.one("SELECT resolution,reached_person,resolved_at FROM escalations WHERE ticket_key='JOOWP-323'")
    assert waiting == {"resolution": "waiting", "reached_person": False, "resolved_at": None}
    waiting_metrics = esc.weekly_metrics(ctx.db, weeks=1)["this_week"]
    assert waiting_metrics["total"] == 2 and waiting_metrics["reached_person"] == 1
    assert waiting_metrics["without_person"] == 0
    assert ctx.db.one("SELECT count(*) AS n FROM owner_decisions WHERE status='asked'")["n"] == 1
    assert set(ctx.db.one("SELECT waiting_tickets FROM owner_decisions WHERE id=%s", (first["decision_id"],))["waiting_tickets"]) == {
        "JOOWP-322", "JOOWP-323"}
    assert ctx.db.one("SELECT count(*) AS n FROM escalations WHERE reached_person")["n"] == 1

    # the owner answers on screen: the choice becomes a register entry and the waiting tickets resume
    answered = client.post(f"/v1/decisions/{first['decision_id']}/answer", json={"choice": "recommended", "note": "see support"})
    assert answered.status_code == 200
    entry = ctx.db.one("SELECT * FROM owner_decisions WHERE id=%s", (first["decision_id"],))
    assert entry["status"] == "decided" and entry["decision"] == "Email after 24 hours (see support)"
    assert entry["decided_by"] == "owner@example.test"
    resolved_waiting = ctx.db.one("SELECT resolution,reached_person,answer FROM escalations WHERE ticket_key='JOOWP-323'")
    assert resolved_waiting == {"resolution": "person", "reached_person": True,
                                "answer": "Email after 24 hours (see support)"}
    answered_metrics = esc.weekly_metrics(ctx.db, weeks=1)["this_week"]
    assert answered_metrics["reached_person"] == 2 and answered_metrics["without_person"] == 0
    assert {k for k, _ in [(t[0], 0) for t in ctx.jira.transitions]} >= {"JOOWP-322", "JOOWP-323"}
    assert all(ctx.jira.status_of[k] == "To Do" for k in ("JOOWP-322", "JOOWP-323"))
    assert ("remove", "JOOWP-322", "owner-escalated") in ctx.jira.labels
    # from now on the decision is applied and cited; a person is not asked
    esc.record(ctx.db, "JOOWP-324", "JOOWP", "dev", "run", esc.BUSINESS,
               "Failed payment email: now or after a day?", resolution="open")
    third = esc.triage_decision(ctx, run_of("JOOWP-324"), NEW, "Failed payment email: now or after a day?")
    assert third["action"] == "applied" and f"D{first['decision_id']}" in third["citation"]
    assert third["answer"].startswith("Email after 24 hours")
    assert esc.dev_question(ctx, run_of("JOOWP-324"), "Failed payment email: now or after a day?")["class"] == esc.TAKEN


def test_concurrent_triage_for_duplicate_questions_creates_one_owner_ask(ctx):
    keys = ("JOOWP-325", "JOOWP-326")
    question = "Should a failed payment email go out immediately or after 24 hours?"
    for key in keys:
        put_ticket(ctx, key, "Acceptance criteria:\n- AC1: x")
        esc.record(ctx.db, key, "JOOWP", "dev", f"dev-{key}", esc.BUSINESS, question, resolution="open")
    barrier = threading.Barrier(2)

    def apply(key):
        barrier.wait(timeout=5)
        return esc.triage_decision(ctx, run_of(key), NEW, question)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, keys))
    assert {result["action"] for result in results} == {"asked", "waiting"}
    assert len({result["decision_id"] for result in results}) == 1
    assert ctx.db.one("SELECT count(*) AS n FROM owner_decisions WHERE status='asked'")["n"] == 1
    register_row = ctx.db.one("SELECT waiting_tickets FROM owner_decisions WHERE status='asked'")
    assert set(register_row["waiting_tickets"]) == set(keys)
    assert ctx.db.one("SELECT count(*) AS n FROM escalations WHERE reached_person")["n"] == 1


def test_jira_comment_answer_is_persisted_and_passed_to_the_next_developer_run(client, ctx):
    key = "TESTWP-327"
    question = "Should refunds be available for 14 days or 30 days?"
    put_ticket(ctx, key, "Acceptance criteria:\n- AC1: refund window", status="To Do")
    asked = esc.ask(ctx, key, "TESTWP", question, "Refund policy choice", ["14 days", "30 days"], "30 days",
                    "Refund period", ["refund", "period"])
    esc.record(ctx.db, key, "TESTWP", "triage", "triage-run", esc.BUSINESS, question, "policy choice", "person",
               asked["decision"]["id"], reached_person=True)
    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    ctx.jira.recent_comment_data[key] = [
        {"author": "AI Flow", "created": future.isoformat(),
         "body": "AI Flow · owner decision required\nOptions: 14 days; 30 days. Recommended default: 30 days."},
        {"author": "Owner", "account_id": ctx.jira.jira_account_id,
         "created": (future + timedelta(seconds=1)).isoformat(),
         "body": "Use 14 days, not the recommended 30 days."},
    ]
    headers = node_headers(client, "cc")
    claim = client.post("/v1/work/claim", json={"ticket": key, "account": "claude-01"}, headers=headers).json()
    assert claim["claimed"]
    assert claim["triage_question"] == question
    assert claim["triage_answer"] == "Use 14 days, not the recommended 30 days."
    entry = ctx.db.one("SELECT status,decision,decided_by FROM owner_decisions WHERE id=%s", (asked["decision"]["id"],))
    assert entry == {"status": "decided", "decision": "Use 14 days, not the recommended 30 days.",
                     "decided_by": "owner"}
    escalation = ctx.db.one("SELECT resolution,reached_person,answer FROM escalations WHERE ticket_key=%s", (key,))
    assert escalation == {"resolution": "person", "reached_person": True,
                          "answer": "Use 14 days, not the recommended 30 days."}


def test_non_owner_jira_comment_does_not_answer_an_owner_only_question(ctx):
    key = "TESTWP-328"
    question = "Should refunds be available for 14 days or 30 days?"
    put_ticket(ctx, key, "Acceptance criteria:\n- AC1: refund window", status="To Do")
    asked = esc.ask(ctx, key, "TESTWP", question, "Refund policy choice", ["14 days", "30 days"], "30 days",
                    "Refund period", ["refund", "period"])
    esc.record(ctx.db, key, "TESTWP", "triage", "triage-run", esc.BUSINESS, question, "policy choice", "person",
               asked["decision"]["id"], reached_person=True)
    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    ctx.jira.recent_comment_data[key] = [{
        "author": "Collaborator", "account_id": "jira-collaborator-account", "created": future.isoformat(),
        "body": "Use 14 days.",
    }]

    assert esc.owner_answered_on_ticket(ctx, key) is None
    pending = ctx.db.one("SELECT status,decision,decided_by FROM owner_decisions WHERE id=%s",
                         (asked["decision"]["id"],))
    assert pending == {"status": "asked", "decision": "", "decided_by": ""}


def test_jira_comment_reader_preserves_stable_account_ids_for_owner_checks():
    def respond(request):
        if request.url.path.endswith("/myself"):
            return httpx.Response(200, json={"accountId": "jira-owner-account"})
        return httpx.Response(200, json={"comments": [{
            "author": {"displayName": "Owner", "accountId": "jira-owner-account"},
            "created": "2026-10-23T12:00:00.000+0000",
            "body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "Use 14 days."}]}]},
        }]})

    client = httpx.Client(base_url="https://jira.example.test", transport=httpx.MockTransport(respond))
    jira = Jira("https://jira.example.test", client=client)
    assert jira.current_account_id() == "jira-owner-account"
    assert jira.recent_comments("TESTWP-1")[0]["account_id"] == "jira-owner-account"
    client.close()


def test_the_triage_worker_asks_once_with_options_and_does_not_alert_a_second_time(client, ctx, tmp_path, monkeypatch):
    headers = node_headers(client, "node1")
    alerts = []
    monkeypatch.setattr(notify, "send", lambda severity, title, detail, link, **k: alerts.append(detail) or ["fake"])
    answer = ("DECISION: ESCALATE\nCLASS: new_business_decision\nWHY: The refund window is a product decision.\n"
              "OPTIONS:\n- 14 days\n- 30 days\nRECOMMENDED: 30 days\nTOPIC: Refund window\nKEYWORDS: refund, window\n")
    for index, key in enumerate(("TESTWP-80", "TESTWP-81")):
        add_ticket(ctx.db, key, status="Needs owner")
        ctx.jira.status_of[key] = "Needs owner"
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary)"
                   " VALUES (%s,%s,'TESTWP','dev','claude-01','claude','cc',1,'finished','blocked',%s)",
                   (str(uuid.uuid4()), key, "What is the refund window for customers?"))
        esc.dev_question(ctx, run_of(key), "What is the refund window for customers?")
        claim = client.post("/v1/work/claim", json={"ticket": key, "account": "codex-01", "step": "triage"}, headers=headers).json()
        assert claim["claimed"] and claim["owner_register"] and claim["owner_register"][0]["title"]
        monkeypatch.setattr(triage, "Git", lambda *a, **k: FakeGit("x"))
        monkeypatch.setattr(triage, "get_adapter", lambda _p, text=answer: FakeAdapter(text))
        claimed = Claimed(run_id=claim["run_id"], fence=claim["fence"], run_token=claim["run_token"], account="codex-01",
                          provider="codex", model=claim["model"], effort=claim["effort"], ticket=claim["ticket"],
                          project=claim["project"], identity=tuple(claim["identity"]), step="triage",
                          last_dev_summary=claim["last_dev_summary"], owner_register=claim["owner_register"])
        result = triage.run_triage(ApiHarness(client, headers), claimed, str(tmp_path), "http://control.test", 30, [])
        assert result["outcome"] == "escalated"
        comment = ctx.jira.comments[-1][1]
        assert "Options: 14 days; 30 days" in comment and "Recommended default: 30 days" in comment
        client.post(f"/v1/runs/{claim['run_id']}/result", json=result, headers=headers)
        client.post(f"/v1/runs/{claim['run_id']}/release", json={"fence": claim["fence"]}, headers=headers)
        if index == 0:
            assert len(alerts) == 1 and "Options: 14 days" in alerts[0] and "Recommended default: 30 days" in alerts[0]
    assert len(alerts) == 1  # the second ticket joined the question that was already asked
    assert "already asked once" in ctx.jira.comments[-1][1]
    assert ctx.db.one("SELECT count(*) AS n FROM owner_decisions WHERE status='asked'")["n"] == 1


def test_a_class_that_never_needs_a_person_cannot_be_escalated(ctx):
    put_ticket(ctx, "JOOWP-330", "Acceptance criteria:\n- AC1: x")
    esc.record(ctx.db, "JOOWP-330", "JOOWP", "dev", "run", esc.MISSING, "needs a browser", resolution="open")
    refused = esc.triage_decision(ctx, run_of("JOOWP-330"), {"decision": "ESCALATE", "class": esc.MISSING,
                                                             "why": "needs a browser", "options": ["a", "b"],
                                                             "recommended": "a"}, "q")
    assert refused["action"] == "refused" and "never reaches a person" in refused["reason"]
    assert ctx.db.one("SELECT count(*) AS n FROM owner_decisions WHERE status='asked'")["n"] == 0


@pytest.mark.parametrize("cls", esc.PERSON_CLASSES)
def test_triage_cannot_answer_a_person_facing_raised_class(ctx, cls):
    key = "JOOWP-331" if cls == esc.BUSINESS else "JOOWP-332"
    put_ticket(ctx, key, "Acceptance criteria:\n- AC1: x")
    esc.record(ctx.db, key, "JOOWP", "dev", "run", cls, "the stored owner question", resolution="open")
    result = esc.triage_decision(ctx, run_of(key), {"decision": "ANSWER", "answer": "AI chose this",
                                                    "why": "repository answer", "class": esc.AMBIGUITY},
                                 "a different question the repository can answer")
    assert result["action"] == "refused" and result["class"] == cls
    row = ctx.db.one("SELECT class,question,resolution,reached_person FROM escalations WHERE ticket_key=%s", (key,))
    assert row == {"class": cls, "question": "the stored owner question", "resolution": "open", "reached_person": False}


@pytest.mark.parametrize("cls", [c for c in esc.CLASSES if c not in esc.PERSON_CLASSES])
def test_triage_cannot_escalate_a_raised_class_that_never_reaches_a_person(ctx, cls):
    key = f"JOOWP-{340 + esc.CLASSES.index(cls)}"
    put_ticket(ctx, key, "Acceptance criteria:\n- AC1: x")
    esc.record(ctx.db, key, "JOOWP", "dev", "run", cls, "the stored question", resolution="open")
    result = esc.triage_decision(ctx, run_of(key), {"decision": "ESCALATE", "class": esc.BUSINESS,
                                                    "why": "owner should choose", "options": ["a", "b"],
                                                    "recommended": "a"}, "spoofed person-facing question")
    assert result["action"] == "refused" and result["class"] == cls
    assert ctx.db.one("SELECT resolution,reached_person FROM escalations WHERE ticket_key=%s", (key,)) == {
        "resolution": "open", "reached_person": False}
    assert ctx.db.one("SELECT count(*) AS n FROM owner_decisions WHERE status='asked'")["n"] == 0


def test_the_owner_can_record_and_retire_a_decision_on_screen(client, ctx):
    owner(client, ctx)
    added = client.post("/v1/decisions", json={"title": "Default theme is dark", "decision": "Dark is the default theme.",
                                               "keywords": ["default theme", "dark"]})
    assert added.status_code == 200
    decision_id = added.json()["id"]
    assert any(d["id"] == decision_id for d in added.json()["register"]["decisions"])
    assert esc.classify(ctx.db, "Should the default theme be dark or light?").cls == esc.TAKEN
    assert client.post("/v1/decisions", json={"title": "Bad", "decision": "pattern", "patterns": ["("]}).status_code == 400
    assert client.post(f"/v1/decisions/{decision_id}/retire").status_code == 200
    assert esc.classify(ctx.db, "Should the default theme be dark or light?").cls != esc.TAKEN
    client.cookies.clear()
    assert client.post("/v1/decisions", json={"title": "Nope", "decision": "Nope"}).status_code == 401
    owner(client, ctx, "lead")
    assert client.post("/v1/decisions", json={"title": "Nope", "decision": "Nope"}).status_code == 403
    assert client.get("/v1/escalations").status_code == 200


# --- AC4: an unreachable criterion is amended on the ticket, in the register, and the ticket goes back to work ------
def test_unreachable_criterion_is_amended_on_the_ticket_and_in_the_register(ctx):
    description = "Acceptance criteria:\n- AC1: the build passes\n- AC4: a smoke test on the Dev VM shows the login page\n- AC5: docs"
    put_ticket(ctx, "JOOWP-301", description)
    result = esc.dev_question(ctx, run_of("JOOWP-301"), FIXTURE["escalations"][7]["text"])
    assert result["class"] == esc.UNREACHABLE and result["resolved"] and result["amended"]
    assert result["outcome"] == "auto_answered"
    new_description = ctx.db.one("SELECT description FROM tickets WHERE key='JOOWP-301'")["description"]
    assert ctx.jira.descriptions[-1] == ("JOOWP-301", new_description)  # the ticket itself in Jira
    line = next(l for l in new_description.splitlines() if l.startswith("- AC4"))
    assert "[narrowed" in line and "Original: a smoke test on the Dev VM" in line
    assert "- AC1: the build passes" in new_description and "- AC5: docs" in new_description
    entry = ctx.db.one("SELECT * FROM owner_decisions WHERE kind='amendment'")
    assert entry["ticket_key"] == "JOOWP-301" and entry["status"] == "decided" and "AC4 narrowed" in entry["decision"]
    assert f"register D{entry['id']}" in line and result["decision_id"] == entry["id"]
    # the same criterion is amended once
    again = esc.dev_question(ctx, run_of("JOOWP-301"), "AC4 still cannot be met: no Dev VM, the evidence cannot be produced.")
    assert again["resolved"] and ctx.db.one("SELECT count(*) AS n FROM owner_decisions WHERE kind='amendment'")["n"] == 1
    # an amendment never blocks: it is no question for a person
    assert ctx.db.one("SELECT reached_person FROM escalations WHERE ticket_key='JOOWP-301' ORDER BY id LIMIT 1")["reached_person"] is False


def test_amendment_can_remove_a_criterion_and_triage_can_amend_through_the_register(ctx):
    put_ticket(ctx, "JOOWP-302", "Acceptance criteria:\n- AC1: a\n- AC2: video of the device")
    run = run_of("JOOWP-302")
    esc.record(ctx.db, "JOOWP-302", "JOOWP", "dev", "run", esc.UNREACHABLE,
               "AC2 requires video of a device; no device exists in this environment", resolution="open")
    missing = esc.triage_decision(ctx, run, {"decision": "AMEND", "criterion": "AC9", "narrowed": "", "why": "x"}, "q")
    assert missing["action"] == "refused"
    done = esc.triage_decision(ctx, run, {"decision": "AMEND", "criterion": "AC2", "narrowed": "REMOVED",
                                          "why": "No device exists in the fleet."}, "AC2 needs a video of a device")
    assert done["action"] == "amended" and done["summary"] == "AC2 removed"
    assert "[removed" in ctx.db.one("SELECT description FROM tickets WHERE key='JOOWP-302'")["description"]
    assert esc.amend_description("no criteria here", "AC1", 1, "") is None
    description, summary = esc.amend_description("- AC3: needs a device", "AC3", 77, "devices", today="2026-10-23")
    assert summary == "AC3 narrowed"
    assert esc.amend_description(description, "AC3", 77, "devices", today="2026-10-23") == (description, summary)


def test_failed_jira_amendment_can_be_retried(ctx, monkeypatch):
    key = "JOOWP-328"
    original = "Acceptance criteria:\n- AC4: capture a screenshot on the Dev VM"
    put_ticket(ctx, key, original)
    real_set_description = ctx.jira.set_description
    calls = {"n": 0}

    def fail_once(ticket, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Jira temporarily unavailable")
        real_set_description(ticket, text)

    monkeypatch.setattr(ctx.jira, "set_description", fail_once)
    with pytest.raises(RuntimeError, match="Jira temporarily unavailable"):
        esc.amend_ticket(ctx, key, "JOOWP", "AC4", "dev_vm", "No Dev VM exists")
    pending = ctx.db.one("SELECT id,decision FROM owner_decisions WHERE kind='amendment' AND ticket_key=%s", (key,))
    assert pending and pending["decision"] == "pending"
    assert ctx.db.one("SELECT description FROM tickets WHERE key=%s", (key,))["description"] == original

    retried = esc.amend_ticket(ctx, key, "JOOWP", "AC4", "dev_vm", "No Dev VM exists")
    assert retried and retried["already"] is False
    assert retried["decision"]["id"] == pending["id"]
    assert calls["n"] == 2
    assert "[narrowed" in ctx.db.one("SELECT description FROM tickets WHERE key=%s", (key,))["description"]
    assert ctx.db.one("SELECT count(*) AS n FROM owner_decisions WHERE kind='amendment' AND ticket_key=%s", (key,))["n"] == 1


def test_saved_owner_decision_retries_a_failed_ticket_resume(ctx, monkeypatch):
    first_key, second_key = "JOOWP-329", "JOOWP-330"
    export_decision = {**NEW, "topic": "Export format", "keywords": ["export", "format"]}
    for key in (first_key, second_key):
        add_ticket(ctx.db, key, status="Needs owner", labels=("owner-escalated",))
        ctx.jira.status_of[key] = "Needs owner"
    question = "Should the new export be CSV or JSON by default?"
    for key in (first_key, second_key):
        esc.dev_question(ctx, run_of(key), question)
        asked = esc.triage_decision(ctx, run_of(key), export_decision, question)
    assert asked["action"] == "waiting"
    decision_id = asked["decision_id"]

    real_transition = ctx.jira.transition_to
    state = {"failed": False}

    def fail_second_transition_once(key, target):
        if key == second_key and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("Jira transition unavailable")
        return real_transition(key, target)

    monkeypatch.setattr(ctx.jira, "transition_to", fail_second_transition_once)
    monkeypatch.setattr(ctx.jira, "recent_comments", lambda key, limit=10: [
        {"body": body} for comment_key, body in ctx.jira.comments if comment_key == key
    ])
    answer = esc.answer_decision(ctx, decision_id, "CSV", "owner selected CSV", "owner")
    assert answer["status"] == "decided" and answer["decision"] == "CSV (owner selected CSV)"
    waiting = ctx.db.one("SELECT resolution,reached_person,answer FROM escalations WHERE ticket_key=%s", (second_key,))
    assert waiting == {"resolution": "resuming", "reached_person": True, "answer": answer["decision"]}
    assert esc.register(ctx.db)["resuming"][0]["resuming_tickets"] == [second_key]
    assert ctx.jira.status_of[second_key] == "Needs owner"

    assert esc.retry_resumptions(ctx) == [second_key]
    resumed = ctx.db.one("SELECT resolution,reached_person,answer FROM escalations WHERE ticket_key=%s", (second_key,))
    assert resumed == {"resolution": "person", "reached_person": True, "answer": answer["decision"]}
    assert ctx.jira.status_of[second_key] == "To Do"
    assert [body for key, body in ctx.jira.comments if key == second_key].count(
        f"AI Flow · owner decision\nWhat: {export_decision['topic']}\nResult: {answer['decision']}\n"
        f"Next: back to To Do; the developer continues with {esc.citation(answer)}") == 1


def test_amendments_appear_in_the_weekly_report_and_never_block(client, ctx):
    owner(client, ctx)
    put_ticket(ctx, "JOOWP-303", "Acceptance criteria:\n- AC2: the deployment is verified on the Dev VM")
    esc.dev_question(ctx, run_of("JOOWP-303"), FIXTURE["escalations"][8]["text"])
    report = client.get("/v1/master/report").json()
    amendments = report["escalations"]["amendments"]
    assert [a["ticket_key"] for a in amendments] == ["JOOWP-303"] and "AC2 narrowed" in amendments[0]["decision"]
    assert ctx.db.one("SELECT count(*) AS n FROM tickets WHERE key='JOOWP-303' AND 'owner-escalated' = ANY(labels)")["n"] == 0


# --- AC5: a refusal explains itself ----------------------------------------------------------------------------------
def test_a_refused_command_names_the_reason_and_the_allowed_alternative():
    refused = decide(POLICY, "Bash", {"command": "p='src/auth/provider.ts'"}, WORKTREE)
    assert refused.decision == "deny"
    assert "not in the project's command policy" in refused.reason
    assert "commands must start with an allowed program" not in refused.reason  # the assignment gets its own advice
    assert "variable assignment" in refused.reason and "Edit tool" in refused.reason
    assert "not a restriction that needs a person" in refused.reason

    other = decide(POLICY, "Bash", {"command": "make build"}, WORKTREE)
    assert "commands must start with an allowed program" in other.reason and "use the Edit or Write tool to change files" in other.reason

    push = decide(POLICY, "Bash", {"command": "git push origin x"}, WORKTREE)
    assert "AI Flow commits and pushes the work" in push.reason

    write = decide(POLICY, "Write", {"file_path": "/etc/passwd"}, WORKTREE)
    assert write.decision == "deny" and "writes are allowed only inside the worktree" in write.reason
    assert "Edit or Write tool with a path inside the worktree" in write.reason

    secret = decide(POLICY, "Read", {"file_path": "/etc/aiflow/secrets/x"}, WORKTREE)
    assert "never available to AI runs" in secret.reason

    read_only = decide_read_only(POLICY, "Write", {"file_path": f"{WORKTREE}/a"}, WORKTREE,
                                 "a triage run reads the base branch and cannot edit files")
    assert "cannot edit files" in read_only.reason and "read-only commands" in read_only.reason

    allowed = decide(POLICY, "Bash", {"command": "composer install"}, WORKTREE)
    assert allowed.decision == "allow" and allowed.reason == "matches the command policy"


def test_the_policy_hook_returns_the_explained_reason_and_a_policy_message_is_not_escalated(client, ctx):
    headers = node_headers(client, "cc")
    add_ticket(ctx.db, "TESTWP-9")
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-9", "account": "claude-01"}, headers=headers).json()
    answer = client.post("/v1/policy/decide", json={"tool_name": "Bash", "cwd": WORKTREE,
                                                     "tool_input": {"command": "p='src/auth/provider.ts' && cat $p"}},
                         headers={"Authorization": f"Bearer {claim['run_token']}"}).json()
    out = answer["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "Edit tool" in out["permissionDecisionReason"] and "Allowed instead" in out["permissionDecisionReason"]
    # an AI that still takes the refusal for a restriction is answered without a person (JOOWP-308)
    raised = client.post("/v1/escalations/raise", json={
        "run_id": claim["run_id"], "fence": claim["fence"],
        "question": "The command policy refused p='src/auth/provider.ts'. Is there a write restriction on that file?"}, headers=headers)
    body = raised.json()
    assert body["class"] == esc.POLICY and body["resolved"]
    assert "Commands must start with an allowed program" in body["answer"] and "Edit or Write tool" in body["answer"]
    master = client.post("/v1/policy/decide", json={"tool_name": "Bash", "tool_input": {"command": "ls"}},
                         headers={"Authorization": "Bearer nothing"}).json()["hookSpecificOutput"]
    assert master["permissionDecision"] == "deny"


# --- AC6: the tail of the failing CI job log through the control API ---------------------------------------------------
def test_the_ci_log_tail_is_read_through_the_control_api(client, ctx, monkeypatch):
    headers = node_headers(client, "cc")
    add_ticket(ctx.db, "TESTWP-9")
    prior_dev = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,pr_url,branch,head_sha,details)"
               " VALUES (%s,'TESTWP-9','TESTWP','dev','claude-01','claude','cc',1,'finished','done',"
               "'https://github.com/jooservices/test-aiflow-wp/pull/9','TESTWP-9-x','sha-TESTWP-9-x',%s::jsonb)",
               (prior_dev, json.dumps({"repository": "jooservices/test-aiflow-wp"})))
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-9", "account": "claude-01"}, headers=headers).json()
    current_run = ctx.db.one("SELECT step,pr_url,branch,head_sha FROM runs WHERE id=%s", (claim["run_id"],))
    assert current_run == {"step": "dev", "pr_url": "https://github.com/jooservices/test-aiflow-wp/pull/9",
                           "branch": "TESTWP-9-x", "head_sha": "sha-TESTWP-9-x"}
    seen = []

    def failing_job_log(repo, sha, lines=200):
        seen.append((repo, sha, lines))
        return {"found": True, "job": "Test (v4)", "workflow": "CI", "conclusion": "failure",
                "url": "https://github.com/jooservices/test-aiflow-wp/actions/runs/1/job/2", "failed_steps": ["Run tests"],
                "lines": 2, "tail": "FAILED tests/v4/test_x.py::test_y\nAssertionError: 1 != 2"}

    ctx.github.pr_data = {9: {"number": 9, "head": {"ref": "TESTWP-9-x", "sha": "sha-TESTWP-9-x",
                              "repo": {"full_name": "jooservices/test-aiflow-wp"}}}}
    monkeypatch.setattr(ctx.github, "failing_job_log", failing_job_log, raising=False)
    got = client.get("/v1/github/ci-log", params={"run_id": claim["run_id"], "fence": claim["fence"],
                                                   "branch": "TESTWP-9-x", "lines": 50}, headers=headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["found"] and body["job"] == "Test (v4)" and "AssertionError: 1 != 2" in body["tail"]
    assert seen == [("test-aiflow-wp", "sha-TESTWP-9-x", 50)]
    wrong_branch = client.get("/v1/github/ci-log", params={"run_id": claim["run_id"], "fence": claim["fence"],
                                                            "branch": "develop"}, headers=headers)
    assert wrong_branch.status_code == 403 and "not the run's pull request branch" in wrong_branch.text
    wrong_sha = client.get("/v1/github/ci-log", params={"run_id": claim["run_id"], "fence": claim["fence"],
                                                         "sha": "another-commit"}, headers=headers)
    assert wrong_sha.status_code == 403 and "not the current head" in wrong_sha.text
    direct_sha = client.get("/v1/github/ci-log", params={"run_id": claim["run_id"], "fence": claim["fence"],
                                                         "sha": "sha-TESTWP-9-x", "lines": 25}, headers=headers)
    assert direct_sha.status_code == 200 and direct_sha.json()["sha"] == "sha-TESTWP-9-x"
    assert seen[-1] == ("test-aiflow-wp", "sha-TESTWP-9-x", 25)
    ctx.github.pr_data[9]["head"]["ref"] = "develop"
    wrong_remote_branch = client.get("/v1/github/ci-log", params={"run_id": claim["run_id"], "fence": claim["fence"],
                                                                   "branch": "TESTWP-9-x"}, headers=headers)
    assert wrong_remote_branch.status_code == 403 and "no longer matches" in wrong_remote_branch.text
    ctx.github.pr_data[9]["head"]["ref"] = "TESTWP-9-x"
    ctx.db.run("UPDATE runs SET pr_url='https://github.com/another-owner/test-aiflow-wp/pull/9' WHERE id=%s",
               (claim["run_id"],))
    wrong_pr = client.get("/v1/github/ci-log", params={"run_id": claim["run_id"], "fence": claim["fence"],
                                                        "branch": "TESTWP-9-x"}, headers=headers)
    assert wrong_pr.status_code == 403 and "does not match the run repository" in wrong_pr.text
    ctx.db.run("UPDATE runs SET pr_url='https://github.com/jooservices/test-aiflow-wp/pull/9' WHERE id=%s",
               (claim["run_id"],))
    assert client.get("/v1/github/ci-log", params={"run_id": claim["run_id"], "fence": claim["fence"] + 1,
                                                    "branch": "b"}, headers=headers).status_code == 409
    assert client.get("/v1/github/ci-log", params={"run_id": claim["run_id"], "fence": claim["fence"]},
                      headers=headers).status_code == 400
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE target='github' AND action='ci-log' AND accepted")["n"] == 2

    # the worker puts the tail in the prompt of the next run on the pull request, so the AI never asks for it
    project = {"key": "TESTWP", "repo": "jooservices/test-aiflow-wp", "base": "develop", "checks": [],
               "work_minutes": {"hard": 60}, "merge": {"method": "squash"}}
    c = Claimed(run_id=claim["run_id"], fence=claim["fence"], run_token="t", account="claude-01", provider="claude",
                model="", effort="", ticket={"key": "TESTWP-9", "project": "TESTWP", "summary": "S", "description": "d",
                                              "labels": [], "priority": "Medium", "type": "Story"},
                project=project, identity=("b", "b@example.test"), branch="TESTWP-9-x", findings="CI failed")
    tail = fetch_ci_log(ApiHarness(client, headers), c, "TESTWP-9-x")
    assert "Job: Test (v4) (failed step: Run tests)" in tail and "AssertionError: 1 != 2" in tail
    prompt = build_prompt(c, "TESTWP-9-x", None, tail)
    assert "never ask for the CI log" in prompt and "AssertionError: 1 != 2" in prompt
    assert fetch_ci_log(ApiHarness(client, headers), Claimed(**{**c.__dict__, "branch": "", "findings": ""}), "b") == ""


def test_ci_log_rejects_a_non_developer_run_even_with_a_recorded_pull_request(client, ctx):
    headers = node_headers(client, "cc")
    add_ticket(ctx.db, "TESTWP-10")
    run_id = str(uuid.uuid4())
    lease = ctx.leases.claim("TESTWP-10", run_id, ctx.settings.lease_ms)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,token_hash,pr_url,branch,head_sha)"
               " VALUES (%s,'TESTWP-10','TESTWP','verify','claude-01','claude','cc',%s,'token',"
               "'https://github.com/jooservices/test-aiflow-wp/pull/10','TESTWP-10-x','commit-10')",
               (run_id, lease.fence))
    response = client.get("/v1/github/ci-log", params={"run_id": run_id, "fence": lease.fence,
                                                        "branch": "TESTWP-10-x"}, headers=headers)
    assert response.status_code == 403 and "only to a developer run" in response.text


def test_github_app_reads_the_tail_of_the_first_failing_job(monkeypatch):
    log = "\n".join(f"2026-10-23T10:00:{i:02d}.000Z line {i}" for i in range(60)) + "\n\x1b[31mFAILED\x1b[0m test_y\n"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": [
                {"id": 7, "name": "CI", "status": "completed", "conclusion": "success"},
                {"id": 8, "name": "CI", "status": "completed", "conclusion": "failure"}]})
        if path.endswith("/actions/runs/8/jobs"):
            return httpx.Response(200, json={"jobs": [
                {"id": 1, "name": "Lint", "conclusion": "success"},
                {"id": 2, "name": "Test (v4)", "conclusion": "failure", "html_url": "https://github.com/x/y/job/2",
                 "steps": [{"name": "Checkout", "conclusion": "success"}, {"name": "Run tests", "conclusion": "failure"}]}]})
        if path.endswith("/actions/jobs/2/logs"):
            return httpx.Response(200, text=log)
        return httpx.Response(404)

    app = GitHubApp.__new__(GitHubApp)
    app.org = "jooservices"
    app.c = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    app.api = lambda method, path, body=None, repo=None, ok=(200,), permissions=None: app.c.request(method, path).json()
    app.token = lambda repo=None, permissions=None: ("tok", None)
    got = app.failing_job_log("repo", "abc123", lines=5)
    assert got["found"] and got["job"] == "Test (v4)" and got["failed_steps"] == ["Run tests"]
    assert got["lines"] == 5 and got["tail"].splitlines()[-1] == "FAILED test_y" and "\x1b" not in got["tail"]
    assert "T10:00" not in got["tail"]  # timestamps are dropped
    app.api = lambda *a, **k: {"workflow_runs": []}
    assert app.failing_job_log("repo", "abc123") == {"found": False, "job": "", "tail": "",
                                                       "message": "no failing GitHub Actions job for this commit"}


# --- AC7: escalations per week by class and project, share without a person, top three causes ---------------------------
def test_weekly_metrics_by_class_and_project_with_the_share_resolved_without_a_person(ctx):
    now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    last_week = now - timedelta(days=7)

    def row(ticket, project, cls, when, resolution="register", person=False):
        ctx.db.run("INSERT INTO escalations (ticket_key,project,class,resolution,reached_person,created_at)"
                   " VALUES (%s,%s,%s,%s,%s,%s)", (ticket, project, cls, resolution, person, when))

    for i in range(4):
        row(f"JOOWP-{i}", "JOOWP", esc.MISSING, now - timedelta(hours=i), "machine")
    row("JAIF-1", "JAIF", esc.POLICY, now, "policy")
    row("JAIF-2", "JAIF", esc.TAKEN, now)
    row("JAIF-3", "JAIF", esc.BUSINESS, now, "person", True)
    row("JAIF-4", "JAIF", esc.UNREACHABLE, last_week, "amendment")
    for i in range(10):
        row(f"JAIF-1{i}", "JAIF", esc.TAKEN, last_week - timedelta(days=i % 3))
    m = esc.weekly_metrics(ctx.db, weeks=4, now=now)
    current = m["this_week"]
    assert current["total"] == 7 and current["reached_person"] == 1 and current["without_person"] == 6
    assert current["share_without_person_pct"] == 85.7
    assert current["by_class"][esc.MISSING] == 4 and current["by_project"]["JOOWP"][esc.MISSING] == 4
    assert current["by_project"]["JAIF"][esc.BUSINESS] == 1
    previous = m["weeks"][-2]
    assert previous["total"] == 11 and previous["by_class"][esc.TAKEN] == 10
    assert m["falling"] is True  # 7 this week against 11 last week
    top = m["top_causes"]
    assert [t["class"] for t in top] == [esc.TAKEN, esc.MISSING, esc.POLICY]
    assert [t["count"] for t in top] == [11, 4, 1]
    assert all(t["fix"] for t in top) and top[0]["reaches_person"] is False
    assert len(m["weeks"]) == 4 and set(m["classes"]) == set(esc.CLASSES)


def test_command_center_and_weekly_report_show_the_escalation_numbers(client, ctx):
    owner(client, ctx)
    for cls in (esc.MISSING, esc.MISSING, esc.BUSINESS):
        esc.record(ctx.db, "JAIF-9", "JAIF", "dev", "r", cls, "q", "", "person" if cls == esc.BUSINESS else "machine",
                   reached_person=cls == esc.BUSINESS)
    overview = client.get("/v1/escalations").json()
    assert overview["metrics"]["this_week"]["total"] == 3
    assert overview["metrics"]["top_causes"][0]["class"] == esc.MISSING
    assert {"decisions", "asked", "amendments"} <= set(overview["register"])
    assert len(overview["register"]["decisions"]) >= 5  # the seeded decisions
    report = client.get("/v1/master/report").json()["escalations"]
    assert report["top_causes"][0]["fix"] and report["this_week"]["share_without_person_pct"] == 66.7
    from aiflow.v4.control.ui import __file__ as ui_init
    page = (Path(ui_init).parent / "console.html").read_text()
    assert "Escalations and owner decisions" in page and "/v1/escalations" in page and "/v1/decisions/" in page
    client.cookies.clear()
    assert client.get("/v1/escalations").status_code == 401


def test_prompts_explain_the_classes_and_the_option_format():
    root = Path(__file__).resolve().parents[2] / "prompts" / "v4"
    triage_prompt, dev_prompt = (root / "triage.md").read_text(), (root / "dev.md").read_text()
    for text in ("missing tool or environment", "policy or permission message", "unreachable acceptance criterion",
                 "ambiguity the repository can answer", "decision already taken", "new business decision",
                 "irreversible or production action", "DECISION: AMEND", "OPTIONS:", "RECOMMENDED:", "{register}"):
        assert text in triage_prompt
    assert "policy message about that one call" in dev_prompt and "never ask for it" in dev_prompt
