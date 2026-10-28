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


def test_repeated_mistake_matches_an_earlier_case_on_another_ticket_in_the_same_project(db, monkeypatch):
    no_embedding(monkeypatch)
    earlier = knowledge.write_study_case(
        db, project="ALPHA", title="Earlier checkout failure", situation="Checkout rejected a request.",
        cause="nonce validation is missing from the checkout route", fix="Validate the nonce at the request boundary.",
        prevention="Keep the nonce check in the request boundary.", source_type="event", source_ref="alpha-event-1",
        ticket_key="ALPHA-10", run_id=str(uuid.uuid4()))
    assert earlier and earlier["repeated_mistake"] is False

    current_run = str(uuid.uuid4())
    repeated = knowledge.write_study_case(
        db, project="ALPHA", title="Later checkout failure", situation="A different ticket hit the same issue.",
        cause="nonce validation is missing from the checkout route", fix="Validate the nonce at the request boundary.",
        prevention="Keep the nonce check in the request boundary.", source_type="event", source_ref="alpha-event-2",
        ticket_key="ALPHA-11", run_id=current_run)
    assert repeated and repeated["repeated_mistake"] is True

    current_only_run = str(uuid.uuid4())
    current_only_first = knowledge.write_study_case(
        db, project="ALPHA", title="First note from current run", situation="A new issue was found.",
        cause="the retry loop ignores the response deadline", fix="Stop retries at the deadline.",
        prevention="Keep the request deadline in retry policy.", source_type="event", source_ref="alpha-current-1",
        ticket_key="ALPHA-12", run_id=current_only_run)
    current_only_again = knowledge.write_study_case(
        db, project="ALPHA", title="Second note from current run", situation="The same run captured it again.",
        cause="the retry loop ignores the response deadline", fix="Stop retries at the deadline.",
        prevention="Keep the request deadline in retry policy.", source_type="event", source_ref="alpha-current-2",
        ticket_key="ALPHA-12", run_id=current_only_run)
    assert current_only_first and current_only_first["repeated_mistake"] is False
    assert current_only_again and current_only_again["repeated_mistake"] is False


def test_general_case_delivered_to_project_counts_as_an_earlier_mistake(db, monkeypatch):
    no_embedding(monkeypatch)
    cause = "the deployment marker is not checked before retrying"
    prior_run = str(uuid.uuid4())
    shared = knowledge.save_item(
        db, scope="general", kind="study_case", title="Deployment marker lesson",
        content=f"Situation: a deployment retry failed. Cause: {cause}. Fix: check the marker before retrying.",
        mistake_signature=knowledge._signature(cause), approved=True, approved_by="Admin")
    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-40", query=cause, run_id=prior_run)
    assert shared["id"] in {item["id"] for item in pack["study_cases"]}

    later = knowledge.write_study_case(
        db, project="ALPHA", title="Deployment marker repeated", situation="Another ticket retried too early.",
        cause=cause, fix="Check the marker before retrying.", prevention="Keep the marker check before retry.",
        source_type="event", source_ref="alpha-deploy-event", ticket_key="ALPHA-41",
        run_id=str(uuid.uuid4()))
    assert later and later["repeated_mistake"] is True


def test_dashboard_scopes_counts_effectiveness_and_rework_to_project_plus_general(db, monkeypatch):
    no_embedding(monkeypatch)
    general = knowledge.save_item(db, scope="general", kind="guide", title="Shared dashboard guidance",
                                  content="Share safe lessons across projects.")
    alpha = knowledge.save_item(db, project="ALPHA", kind="guide", title="Alpha dashboard guidance",
                                content="Alpha-only release steps.")
    bravo = knowledge.save_item(db, project="BRAVO", kind="guide", title="Bravo dashboard guidance",
                                content="Bravo-only deployment steps.")
    db.run("UPDATE knowledge_items SET repeated_mistake=true WHERE id IN (%s,%s,%s)",
           (general["id"], alpha["id"], bravo["id"]))

    for project in ("ALPHA", "ALPHA", "", "BRAVO", "BRAVO", "BRAVO"):
        db.run("INSERT INTO knowledge_context_packs (run_id,project,ticket_key,item_count) VALUES (%s,%s,%s,0)",
               (str(uuid.uuid4()), project, f"{project or 'GLOBAL'}-ticket"))

    for project, ticket in (("ALPHA", "ALPHA-20"), ("ALPHA", "ALPHA-20"),
                            ("", "global:incident"), ("BRAVO", "BRAVO-1"),
                            ("BRAVO", "BRAVO-2"), ("BRAVO", "BRAVO-2")):
        add_run(db, ticket, project, "verify", "rework", "Verification requested rework")

    dashboard = knowledge.dashboard(db, "ALPHA")
    assert {row["project"] for row in dashboard["counts"]} == {"ALPHA", "general"}
    assert {item["id"] for item in dashboard["items"]} == {general["id"], alpha["id"]}
    stats = dashboard["effectiveness"]
    assert stats["runs_with_context_pack"] == 3
    assert stats["known_mistakes_repeated"] == 2
    assert len(stats["rework_by_week"]) == 1
    assert stats["rework_by_week"][0]["rounds"] == 3
    assert stats["rework_by_week"][0]["tickets"] == 2
    assert float(stats["rework_by_week"][0]["rounds_per_ticket"]) == 1.5


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


def test_first_project_failure_is_shared_to_second_project_run_and_avoided(db, monkeypatch):
    no_embedding(monkeypatch)
    add_ticket(db, "ALPHA-1", "ALPHA")
    first_run = add_run(db, "ALPHA-1", "ALPHA", "verify", "rework", "Checkout nonce validation failed",
                        {"findings": "nonce validation is missing from the checkout route"})
    knowledge.capture_run(db, first_run)
    first = db.one("SELECT * FROM knowledge_items WHERE kind='study_case' AND run_id=%s", (first_run,))
    assert first and first["project"] == "ALPHA"

    shared = knowledge.moderate_item(db, first["id"], action="correct", actor="Admin",
                                     title="Shared nonce validation lesson",
                                     content="Situation: checkout failed. Cause: nonce validation was missing. "
                                             "Fix: validate at the request boundary. Avoid next time: check the "
                                             "nonce before accepting checkout.", scope="general", project="", epic="")
    assert db.one("SELECT status FROM knowledge_items WHERE id=%s", (first["id"],))["status"] == "retired"

    add_ticket(db, "BRAVO-1", "BRAVO")
    second_run = str(uuid.uuid4())
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status) "
           "VALUES (%s,'BRAVO-1','BRAVO','dev','codex-01','node1',1,'running')", (second_run,))
    pack = knowledge.context_pack(db, project="BRAVO", ticket="BRAVO-1", query="checkout request validation",
                                  run_id=second_run)
    assert shared["scope"] == "general" and shared["project"] == ""
    assert shared["id"] in {item["id"] for item in pack["study_cases"]}
    assert "Avoid next time" in next(item["content"] for item in pack["study_cases"]
                                      if item["id"] == shared["id"])
    from aiflow.v4.worker.dev import Claimed, build_prompt
    run = Claimed(run_id=second_run, fence=1, run_token="", account="codex-01", provider="codex",
                  model="", effort="", ticket={"key": "BRAVO-1", "project": "BRAVO",
                  "summary": "Validate the new checkout path", "description": "Checkout request handling"},
                  project={"repo": "org/bravo", "base": "main", "checks": []}, identity=("AI", "ai"),
                  context_pack=pack)
    prompt = build_prompt(run, "BRAVO-1-checkout")
    assert "validate at the request boundary" in prompt
    assert "Avoid next time" in prompt
    summary = "Validated nonce at the request boundary before accepting checkout"
    db.run("UPDATE runs SET status='finished',outcome='done',summary=%s,details=%s,ended_at=now() WHERE id=%s",
           (summary, J({"why": "The shared study case identified the missing validation."}), second_run))
    knowledge.capture_run(db, second_run)
    assert summary in db.one("SELECT summary FROM runs WHERE id=%s", (second_run,))["summary"]


def test_correcting_an_item_retires_old_text_from_context_pack_and_search(db, monkeypatch):
    no_embedding(monkeypatch)
    old_text = "oldcopperphrase is the obsolete retry rule"
    old = knowledge.save_item(db, project="ALPHA", kind="guide", title="Retry handling guide",
                              content=old_text, approved=True, approved_by="Reviewer")
    revised = knowledge.moderate_item(db, old["id"], action="correct", actor="Admin",
                                      content="Use bounded retries with jitter and stop after the deadline.")

    assert db.one("SELECT status FROM knowledge_items WHERE id=%s", (old["id"],))["status"] == "retired"
    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-30")
    assert revised["id"] in {item["id"] for item in pack["items"]}
    assert old_text not in "\n".join(item["content"] for item in pack["items"])
    search = knowledge.search_items(db, project="ALPHA", q="oldcopperphrase obsolete retry rule")
    assert old["id"] not in {item["id"] for item in search}
    assert all(old_text not in item["content"] for item in search)


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


def test_knowledge_learning_screen_is_verified_in_a_browser(ctx, monkeypatch):
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error, sync_playwright
    import socket
    import threading
    import time
    import uvicorn

    from aiflow.v4.control import knowledge, login
    from aiflow.v4.control.api import create_app
    from aiflow.v4.control.ui import mount_ui

    no_embedding(monkeypatch)
    lesson = knowledge.save_item(ctx.db, project="TESTWP", kind="guide", title="Browser verification lesson",
                                 content="Use the project dashboard before the next release.",
                                 source_type="run", source_ref="browser-run", approved=True,
                                 approved_by="Browser Reviewer")
    knowledge.save_item(ctx.db, project="TESTWP", kind="study_case", title="Browser repeated mistake",
                        content="Check the deployment marker before retrying.", approved=True,
                        approved_by="Browser Reviewer", repeated_mistake=True)
    knowledge.save_item(ctx.db, project="TESTWP", kind="guide", title="Browser pending review",
                        content="Confirm the release artifact checksum.")
    knowledge.context_pack(ctx.db, project="TESTWP", ticket="TESTWP-930", run_id=str(uuid.uuid4()))
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,summary,ended_at) "
               "VALUES (%s,'TESTWP-931','TESTWP','verify','codex-01','node1',1,'finished','rework',"
               "'Browser rework example',now())", (str(uuid.uuid4()),))
    assert ctx.db.one("SELECT handed_count FROM knowledge_items WHERE id=%s", (lesson["id"],))["handed_count"] == 1

    try:
        playwright = sync_playwright().start()
    except Error as exc:
        pytest.skip(f"Playwright cannot start on this node: {exc}")
    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Error as exc:
            pytest.skip(f"Playwright Chromium cannot launch on this node: {exc}")

        ctx.settings.console_login = True
        ctx.settings.console_session_secret = "browser-test-session-secret-at-least-32-bytes"
        ctx.settings.console_require_mfa = False  # this browser test covers the rest of the console login flow
        ctx.settings.public_url = ""
        password = login.seed_user(ctx.db, "knowledge-browser@example.test", "super_admin", "Knowledge Browser")
        assert password
        app = create_app(ctx)
        mount_ui(app, ctx)
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(128)
        base_url = f"http://127.0.0.1:{server_socket.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        server_thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)
        try:
            server_thread.start()
            deadline = time.monotonic() + 10
            while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "Knowledge browser test server did not start"

            page = browser.new_page()
            page.goto(base_url + "/login?next=%2Fcc", wait_until="domcontentloaded")
            page.get_by_label("Email").fill("knowledge-browser@example.test")
            page.get_by_label("Password").fill(password)
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url(base_url + "/cc")
            assert page.locator("#tab-knowledge").count() == 1, (
                f"console shell missing at {page.evaluate('location.pathname')} ({page.title()})")
            page.get_by_role("tab", name="Knowledge").click()
            page.get_by_role("heading", name="Learning and study cases").wait_for(state="visible", timeout=10000)
            page.get_by_placeholder("Search by meaning or words").wait_for(state="visible")
            for heading in ("Counts by project and kind", "Newest", "Most used"):
                page.get_by_role("heading", name=heading, exact=True).wait_for(state="visible")
            page.get_by_text("Browser verification lesson", exact=True).wait_for(state="visible")
            page.get_by_text("run · browser-run", exact=False).wait_for(state="visible")
            page.get_by_text("approved by Browser Reviewer", exact=False).first.wait_for(state="visible")
            page.get_by_text("handed to 1 runs", exact=False).first.wait_for(state="visible")
            page.get_by_text("Runs with context pack: 1", exact=False).wait_for(state="visible")
            page.get_by_text("Known mistakes repeated: 1", exact=False).wait_for(state="visible")
            page.get_by_text("rounds per ticket", exact=False).wait_for(state="visible")
            for action in ("Correct", "Approve / review", "Flag for review", "Retire", "Merge duplicate…"):
                page.get_by_role("button", name=action, exact=True).first.wait_for(state="visible")
            page.get_by_placeholder("Search by meaning or words").fill("project dashboard before release")
            page.locator("#view-knowledge [data-act='knowledge-search']").click()
            page.get_by_role("heading", name="Knowledge items · 1", exact=True).wait_for(state="visible")
        finally:
            browser.close()
            server.should_exit = True
            if server_thread.is_alive():
                server_thread.join(timeout=5)
            server_socket.close()
    finally:
        playwright.stop()
