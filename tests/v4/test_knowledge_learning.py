"""Automatic knowledge capture, context packs, isolation, moderation and filtering."""
from __future__ import annotations

import uuid

import pytest

from aiflow.v4.control import knowledge
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.db import J


SESSION_SECRET = "knowledge-learning-test-session-secret-32-bytes"


def no_embedding(monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)


def add_ticket(db, key: str, project: str, *, status: str = "To Do", parent: str = ""):
    db.run("INSERT INTO tickets (key,project,status,summary,description,parent_key) VALUES (%s,%s,%s,%s,%s,%s)",
           (key, project, status, f"Summary {key}", "Business reason and acceptance criteria", parent or None))


def add_run(db, key: str, project: str, step: str, outcome: str, summary: str, details=None, pr_url="") -> str:
    run_id = str(uuid.uuid4())
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,summary,details,pr_url,ended_at) "
           "VALUES (%s,%s,%s,%s,'codex-01','node1',1,'finished',%s,%s,%s,%s,now())",
           (run_id, key, project, step, outcome, summary, J(details or {}), pr_url))
    return run_id


def test_finished_run_and_closed_ticket_are_saved_with_sources_and_pr_links(db, monkeypatch):
    no_embedding(monkeypatch)
    db.run("INSERT INTO tickets (key,project,status,type,summary,description) VALUES "
           "('ALPHA-1','ALPHA','Done','Task','Improve checkout','Reduce failed checkout attempts')")
    run_id = add_run(db, "ALPHA-1", "ALPHA", "dev", "done", "Added nonce validation",
                     {"why": "The checkout endpoint accepted unsigned requests.",
                      "decisions": ["Keep the existing route"], "changed_files": ["src/checkout.py"]},
                     "https://github.com/org/repo/pull/42")

    saved = knowledge.capture_run(db, run_id, "https://jira.example.test")
    assert saved["kind"] == "decision" and saved["run_id"] == run_id
    assert saved["source_url"] == "https://jira.example.test/browse/ALPHA-1"
    assert saved["pr_url"] == "https://github.com/org/repo/pull/42"
    assert "Keep the existing route" in saved["content"]
    assert "src/checkout.py" in saved["content"]

    assert knowledge.capture_closed_tickets(db, "https://jira.example.test") == 1
    assert knowledge.capture_closed_tickets(db, "https://jira.example.test") == 0
    closed = db.one("SELECT * FROM knowledge_items WHERE source_type='ticket' AND source_ref='ALPHA-1'")
    assert closed["kind"] == "study_case" and closed["run_id"] == run_id
    assert closed["pr_url"] == "https://github.com/org/repo/pull/42"
    assert "What was done" in closed["content"] and "Why" in closed["content"]


def test_failure_study_case_is_handed_to_next_run_and_repetition_is_counted(db, monkeypatch):
    no_embedding(monkeypatch)
    add_ticket(db, "ALPHA-2", "ALPHA")
    first = add_run(db, "ALPHA-2", "ALPHA", "verify", "rework", "Verify found a missing nonce check",
                    {"findings": "nonce validation is missing from the checkout route"})
    knowledge.capture_run(db, first)
    case = db.one("SELECT * FROM knowledge_items WHERE kind='study_case' AND run_id=%s", (first,))
    assert case and "Situation:" in case["content"] and "Cause:" in case["content"]
    assert "Fix:" in case["content"] and "Avoid next time:" in case["content"]

    next_run = str(uuid.uuid4())
    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-2", query="checkout nonce validation",
                                  run_id=next_run)
    assert any(item["id"] == case["id"] for item in pack["study_cases"])
    assert db.one("SELECT handed_count FROM knowledge_items WHERE id=%s", (case["id"],))["handed_count"] == 1

    second = add_run(db, "ALPHA-2", "ALPHA", "verify", "rework", "The nonce finding returned",
                     {"findings": "nonce validation is missing from the checkout route"})
    knowledge.capture_run(db, second)
    assert db.one("SELECT repeated_mistake FROM knowledge_items WHERE kind='study_case' AND run_id=%s",
                  (second,))["repeated_mistake"] is True
    assert knowledge.dashboard(db, "ALPHA")["effectiveness"]["known_mistakes_repeated"] == 1


def test_global_run_and_incident_resolution_also_leave_general_study_cases(ctx, monkeypatch):
    no_embedding(monkeypatch)
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,summary,ended_at) "
               "VALUES (%s,'master:global','', 'master','codex-01','node1',1,'finished','failed','Watchdog query timed out',now())",
               (run_id,))
    knowledge.capture_run(ctx.db, run_id)
    global_run = ctx.db.one("SELECT * FROM knowledge_items WHERE source_type='run' AND source_ref=%s AND kind='decision'",
                            (run_id,))
    assert global_run and global_run["scope"] == "general"

    from aiflow.v4.control.incidents import close_incident, open_incident
    assert open_incident(ctx, "test_incident", "node-test", "info", "Node restart", "Worker exited unexpectedly")
    case = ctx.db.one("SELECT * FROM knowledge_items WHERE source_type='incident' AND kind='study_case'")
    assert case and "Resolution pending" in case["content"]
    close_incident(ctx, "test_incident", "node-test", "Restarted the worker and confirmed heartbeat")
    updated = ctx.db.one("SELECT content FROM knowledge_items WHERE id=%s", (case["id"],))
    assert "confirmed heartbeat" in updated["content"] and "Avoid next time" in updated["content"]


def test_context_pack_shares_general_items_and_keeps_projects_isolated_and_retirement_effective(db, monkeypatch):
    no_embedding(monkeypatch)
    general = knowledge.save_item(db, scope="general", kind="convention", title="General testing",
                                  content="Run focused checks before wider validation", approved=True,
                                  approved_by="Admin")
    alpha = knowledge.save_item(db, project="ALPHA", kind="convention", title="Alpha rule",
                                content="Alpha uses the checkout fixture", approved=True, approved_by="Admin")
    bravo = knowledge.save_item(db, project="BRAVO", kind="convention", title="Bravo rule",
                                content="Bravo uses the billing fixture", approved=True, approved_by="Admin")
    alpha_pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-3")
    assert {item["id"] for item in alpha_pack["items"]} == {general["id"], alpha["id"]}
    assert bravo["id"] not in {item["id"] for item in alpha_pack["items"]}
    assert {item["id"] for item in knowledge.search_items(db, project="ALPHA")} == {general["id"], alpha["id"]}

    knowledge.moderate_item(db, alpha["id"], action="retire", actor="Admin")
    pack_after_retire = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-4")
    assert alpha["id"] not in {item["id"] for item in pack_after_retire["items"]}
    assert alpha["id"] not in {item["id"] for item in knowledge.search_items(db, project="ALPHA")}
    assert alpha["id"] in {item["id"] for item in knowledge.search_items(db, project="ALPHA", include_retired=True)}


def test_old_guidance_is_flagged_stale_and_can_be_reviewed(db, monkeypatch):
    no_embedding(monkeypatch)
    item = knowledge.save_item(db, project="ALPHA", kind="guide", title="Old release rule",
                               content="Check the release note before tagging.", approved=True, approved_by="Admin")
    db.run("UPDATE knowledge_items SET updated_at=now()-interval '181 days' WHERE id=%s", (item["id"],))
    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-9")
    stale = next(row for row in pack["items"] if row["id"] == item["id"])
    assert stale["status"] == "stale"
    approved = knowledge.moderate_item(db, item["id"], action="approve", actor="Reviewer")
    assert approved["status"] == "active" and approved["approved_by"] == "Reviewer"


def test_first_project_study_case_can_be_curated_as_a_general_lesson_for_a_second_project(db, monkeypatch):
    no_embedding(monkeypatch)
    first = knowledge.save_item(db, project="ALPHA", kind="study_case", title="Checkout nonce mistake",
                                content="Situation: checkout failed. Cause: nonce was not checked. Fix: validate it at the request boundary. Avoid next time: check the nonce before accepting checkout.",
                                source_type="run", source_ref="run-alpha", approved=True,
                                approved_by="AI Flow automatic capture")
    shared = knowledge.moderate_item(db, first["id"], action="correct", actor="Admin",
                                     content=first["content"], scope="general", project="", epic="")
    pack = knowledge.context_pack(db, project="BRAVO", ticket="BRAVO-1", query="checkout request validation")
    assert shared["scope"] == "general" and shared["project"] == ""
    assert shared["id"] in {item["id"] for item in pack["study_cases"]}
    assert "Avoid next time" in next(item["content"] for item in pack["study_cases"]
                                      if item["id"] == shared["id"])
    from aiflow.v4.worker.dev import Claimed, build_prompt
    run = Claimed(run_id=str(uuid.uuid4()), fence=1, run_token="", account="codex-01", provider="codex",
                  model="", effort="", ticket={"key": "BRAVO-1", "project": "BRAVO",
                  "summary": "Validate the new checkout path", "description": "Checkout request handling"},
                  project={"repo": "org/bravo", "base": "main", "checks": []}, identity=("AI", "ai"),
                  context_pack=pack)
    prompt = build_prompt(run, "BRAVO-1-checkout")
    assert "validate it at the request boundary" in prompt
    assert "Avoid next time" in prompt


def test_context_pack_uses_vector_only_study_case_retrieval(db, monkeypatch):
    vector = "[" + ",".join(["1"] + ["0"] * 383) + "]"
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: vector)
    case = knowledge.save_item(db, project="ALPHA", kind="study_case", title="Automated password guessing",
                               content="A credential endpoint was hit by a scripted adversary.",
                               approved=True, approved_by="AI Flow")
    hits = knowledge.search_items(db, project="ALPHA", q="mitigate hostile authentication traffic",
                                  approved_only=True, kinds=("study_case",))
    assert hits and hits[0]["id"] == case["id"]
    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-5",
                                  query="mitigate hostile authentication traffic")
    assert case["id"] in {item["id"] for item in pack["study_cases"]}


def test_work_claim_delivers_stored_pack_into_worker_prompt_and_run_result_is_captured(client, ctx, monkeypatch):
    no_embedding(monkeypatch)
    from conftest import add_ticket
    from test_api import join
    from aiflow.v4.control import memory
    monkeypatch.setattr(memory, "embed_texts", lambda _texts: (_ for _ in ()).throw(RuntimeError("offline")))
    add_ticket(ctx.db, "TESTWP-93")
    convention = knowledge.save_item(ctx.db, project="TESTWP", kind="convention", title="Project convention",
                                      content="Keep checkout validation in the request boundary.", approved=True,
                                      approved_by="Admin")
    auth = join(client, "cc")
    response = client.post("/v1/work/claim", json={"ticket": "TESTWP-93", "account": "claude-01"}, headers=auth)
    assert response.status_code == 200, response.text
    claimed = response.json()
    assert convention["id"] in {item["id"] for item in claimed["context_pack"]["items"]}
    from aiflow.v4.worker.dev import Claimed, build_prompt
    worker_claim = Claimed(run_id=claimed["run_id"], fence=claimed["fence"], run_token="t",
                           account="claude-01", provider="claude", model="opus", effort="",
                           ticket=claimed["ticket"], project=claimed["project"],
                           identity=tuple(claimed["identity"]), context_pack=claimed["context_pack"])
    assert "Project convention" in build_prompt(worker_claim, "TESTWP-93-checkout")

    result = client.post(f"/v1/runs/{claimed['run_id']}/result",
                         json={"fence": claimed["fence"], "outcome": "done", "summary": "Added validation",
                               "details": {"why": "The ticket requires input validation.",
                                           "changed_files": ["src/checkout.py"]}}, headers=auth)
    assert result.status_code == 200, result.text
    stored = ctx.db.one("SELECT * FROM knowledge_items WHERE source_type='run' AND source_ref=%s AND kind='decision'",
                        (claimed["run_id"],))
    assert stored and "src/checkout.py" in stored["content"]


def test_personal_and_secret_content_is_rejected_before_save(db, monkeypatch):
    no_embedding(monkeypatch)
    with pytest.raises(knowledge.Invalid, match="cannot be saved"):
        knowledge.save_item(db, project="ALPHA", kind="guide", title="Token guide",
                            content="access_token=ghp_abcdefghijklmnopqrstuvwxyz1234567890")
    with pytest.raises(knowledge.Invalid, match="cannot be saved"):
        knowledge.save_item(db, project="ALPHA", kind="study_case", title="Contact",
                            content="email: person@example.test")
    with pytest.raises(knowledge.Invalid, match="cannot be saved"):
        knowledge.save_item(db, project="ALPHA", kind="guide", title="Call details",
                            content="Call +1-415-555-0188 after the outage.")
    assert db.one("SELECT count(*) AS n FROM knowledge_items")["n"] == 0

    add_ticket(db, "ALPHA-8", "ALPHA")
    run_id = add_run(db, "ALPHA-8", "ALPHA", "dev", "done",
                     "Stored contact person@example.test and access_token=ghp_abcdefghijklmnopqrstuvwxyz1234567890")
    captured = knowledge.capture_run(db, run_id)
    assert captured is not None
    assert "person@example.test" not in captured["content"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890" not in captured["content"]


def test_knowledge_screen_items_dashboard_and_moderation_routes(client, ctx, monkeypatch):
    no_embedding(monkeypatch)
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session("admin@example.test", "owner", 2_000_000_000,
                                                         SESSION_SECRET))
    created = client.post("/v1/knowledge/items", json={"project": "TESTWP", "scope": "project",
                               "kind": "guide", "title": "Checkout guide", "content": "Keep checkout checks local."})
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]
    listed = client.get("/v1/knowledge/items", params={"project": "TESTWP"})
    assert listed.status_code == 200
    data = listed.json()
    assert data["items"][0]["source_type"] == "manual"
    assert data["items"][0]["approved_by"] == ""
    assert any(row["kind"] == "guide" and row["project"] == "TESTWP" for row in data["counts"])
    assert "runs_with_context_pack" in data["effectiveness"]
    assert client.post(f"/v1/knowledge/items/{item_id}/approve", json={"actor": "Admin"}).status_code == 200
    assert client.post(f"/v1/knowledge/items/{item_id}/retire", json={"actor": "Admin"}).status_code == 200
    retired_search = client.get("/v1/knowledge/items", params={"project": "TESTWP", "q": "checkout", "include_retired": False})
    assert item_id not in {item["id"] for item in retired_search.json()["items"]}
    from aiflow.v4.control.ui import mount_ui
    mount_ui(client.app, client.app.state.ctx)
    page = client.get("/knowledge")
    assert page.status_code == 200
    assert "Learning and study cases" in page.text and "Runs with context pack" in page.text
    assert "Merge duplicate" in page.text and "Approve / review" in page.text


def test_learning_items_api_rejects_personal_data(client, ctx):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session("admin@example.test", "owner", 2_000_000_000,
                                                         SESSION_SECRET))
    response = client.post("/v1/knowledge/items", json={"project": "TESTWP", "kind": "guide",
                              "title": "Contact", "content": "email: person@example.test"})
    assert response.status_code == 422
    assert "cannot be saved" in response.json()["detail"]
    from aiflow.v4.control.ui import mount_ui
    mount_ui(client.app, client.app.state.ctx)
    managed = client.post("/v1/knowledge/resources", json={"type": "guide", "id": "contact-guide",
                            "title": "Contact", "content": "email: person@example.test", "actor": "Admin"})
    assert managed.status_code == 400 and "cannot be saved" in managed.json()["detail"]
