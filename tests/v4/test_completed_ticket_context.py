"""Completed-ticket notes stay searchable while targeted claims omit them."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from aiflow.v4.control import knowledge
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.features.work import claim_extras

_SESSION_SECRET = "completed-ticket-context-test-session-secret"


def test_default_search_and_context_pack_include_completed_ticket_notes(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    note = knowledge.save_item(
        db, project="ALPHA", kind="study_case", title="Completed ticket checkout note",
        content="Completed ticket checkout validation lesson.", source_type="ticket",
        source_ref="ALPHA-620", ticket_key="ALPHA-620", approved=True, approved_by="AI Flow")

    hits = knowledge.search_items(db, project="ALPHA", q="completed ticket checkout validation",
                                  approved_only=True, kinds=("study_case",))
    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-621",
                                  query="completed ticket checkout validation")

    assert note["id"] in {item["id"] for item in hits}
    assert note["id"] in {item["id"] for item in pack["items"]}


def test_authorized_knowledge_api_keeps_completed_ticket_notes_visible(client, ctx, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    ctx.settings.console_session_secret = _SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session("owner@example.test", "owner", 2_000_000_000,
                                                         _SESSION_SECRET))
    note = _item(ctx.db, title="Completed ticket checkout note", content="Completed ticket checkout lesson.",
                 source_type="ticket", source_ref="ALPHA-KNOWLEDGE", ticket_key="ALPHA-KNOWLEDGE")

    response = client.get("/v1/knowledge/items", params={"project": "ALPHA", "q": "completed ticket checkout"})

    assert response.status_code == 200
    assert note["id"] in {item["id"] for item in response.json()["items"]}


def _item(db, *, title, content, kind="study_case", source_type="event", source_ref="",
          project="ALPHA", scope="project", epic="", ticket_key="", approved=True, status="active"):
    return knowledge.save_item(db, project=project, scope=scope, epic=epic, kind=kind, title=title,
                               content=content, source_type=source_type, source_ref=source_ref,
                               ticket_key=ticket_key, approved=approved, approved_by="AI Flow", status=status)


def _run(db, step, ticket="ALPHA-620"):
    run_id = str(uuid.uuid4())
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status) "
           "VALUES (%s,%s,'ALPHA',%s,'codex-01','node1',1,'running')", (run_id, ticket, step))
    return run_id


def _claim_context(db, step, run_id):
    return SimpleNamespace(body=SimpleNamespace(step=step), ctx=SimpleNamespace(db=db),
                           ticket={"key": "ALPHA-620", "project": "ALPHA", "summary": "checkout validation",
                                   "description": "Use checkout validation knowledge."},
                           epic="", run_id=run_id)


def _group_items(pack):
    return [item for name, value in pack.items() if name != "items" and isinstance(value, list) for item in value]


@pytest.mark.parametrize(("step", "exclude"), [("dev", True), ("verify", True), ("qa", True), ("triage", False)])
def test_claim_step_controls_exclusion_and_delivery_accounting(db, monkeypatch, step, exclude):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    run_id = _run(db, step)
    completed = _item(db, title="Completed ticket checkout", content="Completed checkout validation lesson.",
                      source_type="ticket", source_ref="ALPHA-620", ticket_key="ALPHA-620")
    event_case = _item(db, title="Checkout validation case", content="Use checkout validation knowledge at the boundary.",
                       source_ref="alpha-event")
    ticket_guide = _item(db, title="Ticket sourced checkout guide", content="Keep checkout validation local.",
                         kind="guide", source_type="ticket", source_ref="alpha-guide")
    convention = _item(db, title="Checkout convention", content="Validate checkout requests at the boundary.",
                       kind="convention", source_ref="alpha-convention")

    extra = {}
    claim_extras.context_pack(_claim_context(db, step, run_id), extra)
    pack = extra["context_pack"]
    pack_ids = {item["id"] for item in pack["items"]}
    group_ids = {item["id"] for item in _group_items(pack)}
    delivered_ids = {row["item_id"] for row in db.all(
        "SELECT item_id FROM knowledge_deliveries WHERE run_id=%s", (run_id,))}

    assert (completed["id"] not in pack_ids) is exclude
    assert (completed["id"] not in group_ids) is exclude
    assert {event_case["id"], ticket_guide["id"], convention["id"]} <= pack_ids
    assert delivered_ids == pack_ids
    assert db.one("SELECT item_count FROM knowledge_context_packs WHERE run_id=%s", (run_id,))["item_count"] == len(pack_ids)
    counts = {row["id"]: row["handed_count"] for row in db.all(
        "SELECT id,handed_count FROM knowledge_items WHERE id=ANY(%s)",
        ([completed["id"], event_case["id"], ticket_guide["id"], convention["id"]],))}
    assert counts[completed["id"]] == (0 if exclude else 1)
    assert all(counts[item_id] == 1 for item_id in {event_case["id"], ticket_guide["id"], convention["id"]})


def test_excluded_candidates_do_not_consume_the_500_row_limit(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    convention = _item(db, title="Older eligible convention", content="Keep checkout validation explicit.",
                       kind="convention", source_ref="older-convention")
    db.run("UPDATE knowledge_items SET created_at=now()-interval '1 day' WHERE id=%s", (convention["id"],))
    db.run("INSERT INTO knowledge_items (project,scope,kind,title,content,status,approved,source_type,source_ref,"
           "ticket_key,created_at) SELECT 'ALPHA','project','study_case','Completed ticket candidate '||n,"
           "'Checkout validation candidate lesson '||n,'active',true,'ticket','ALPHA-COMP-'||n,'ALPHA-COMP-'||n,"
           "now()+n*interval '1 second' FROM generate_series(1,501) AS n")

    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-621", query="checkout validation",
                                  exclude_completed_tickets=True)

    assert convention["id"] in {item["id"] for item in pack["items"]}


@pytest.mark.parametrize("vector_search", [False, True], ids=["lexical", "vector"])
def test_excluded_notes_do_not_consume_the_eight_search_hits(db, monkeypatch, vector_search):
    vector = "[" + ",".join(["1"] + ["0"] * 383) + "]"
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: vector if vector_search else None)
    eligible = _item(db, title="Copper widget checkout case", content="Copper widget check at checkout.",
                     source_ref="older-copper")
    db.run("UPDATE knowledge_items SET created_at=now()-interval '1 day' WHERE id=%s", (eligible["id"],))
    for index in range(12):
        _item(db, title=f"Copper widget checkout copper widget ticket {index}",
              content="Copper widget checkout copper widget checkout validation lesson.", source_type="ticket",
              source_ref=f"ALPHA-COPPER-{index}", ticket_key=f"ALPHA-COPPER-{index}")

    old_behavior = knowledge.search_items(db, project="ALPHA", q="copper widget checkout", limit=8,
                                          approved_only=True, kinds=("study_case",))
    filtered = knowledge.search_items(db, project="ALPHA", q="copper widget checkout", limit=8,
                                      approved_only=True, kinds=("study_case",),
                                      exclude_completed_tickets=True)

    assert len(old_behavior) == 8
    assert all(item["source_type"] == "ticket" for item in old_behavior)
    assert eligible["id"] not in {item["id"] for item in old_behavior}
    assert eligible["id"] in {item["id"] for item in filtered}
    assert all(not (item["kind"] == "study_case" and item["source_type"] == "ticket") for item in filtered)


def test_scope_approval_status_and_knowledge_visibility_are_preserved(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    project_case = _item(db, title="Project checkout lesson", content="checkout validation project", source_ref="project")
    general_case = _item(db, title="General checkout lesson", content="checkout validation general", source_ref="general",
                         project="", scope="general")
    epic_case = _item(db, title="Epic checkout lesson", content="checkout validation epic", source_ref="epic",
                      epic="ALPHA-EPIC")
    other_epic = _item(db, title="Other epic checkout lesson", content="checkout validation other epic",
                       source_ref="other-epic", epic="ALPHA-OTHER")
    unapproved = _item(db, title="Unapproved checkout lesson", content="checkout validation pending",
                       source_ref="unapproved", approved=False)
    retired = _item(db, title="Retired checkout lesson", content="checkout validation retired",
                    source_ref="retired", status="retired")
    completed = _item(db, title="Completed ticket checkout lesson", content="checkout validation completed",
                      source_type="ticket", source_ref="completed", ticket_key="ALPHA-620")

    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-621", epic="ALPHA-EPIC",
                                  query="checkout validation", exclude_completed_tickets=True)
    pack_ids = {item["id"] for item in pack["items"]}
    screen_ids = {item["id"] for item in knowledge.search_items(db, project="ALPHA", q="checkout validation",
                                                                 approved_only=True, kinds=("study_case",))}

    assert {project_case["id"], general_case["id"], epic_case["id"]} <= pack_ids
    assert not {other_epic["id"], unapproved["id"], retired["id"], completed["id"]} & pack_ids
    assert completed["id"] in screen_ids
    assert unapproved["id"] not in screen_ids and retired["id"] not in screen_ids


def test_empty_filtered_pack_records_zero_deliveries(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    completed = _item(db, title="Completed ticket only", content="A checkout validation lesson.",
                      source_type="ticket", source_ref="only-completed", ticket_key="ALPHA-620")
    run_id = _run(db, "dev")

    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-620", run_id=run_id,
                                  exclude_completed_tickets=True)

    assert pack["items"] == []
    assert _group_items(pack) == []
    assert db.one("SELECT item_count,token_count FROM knowledge_context_packs WHERE run_id=%s", (run_id,)) == {
        "item_count": 0, "token_count": 0}
    assert db.all("SELECT item_id FROM knowledge_deliveries WHERE run_id=%s", (run_id,)) == []
    assert db.one("SELECT handed_count FROM knowledge_items WHERE id=%s", (completed["id"],))["handed_count"] == 0


def test_claim_context_failure_keeps_the_empty_pack_fallback(monkeypatch):
    work = _claim_context(object(), "dev", "run-1")

    def fail(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(claim_extras.knowledge, "context_pack", fail)
    extra = {}

    claim_extras.context_pack(work, extra)

    assert extra["context_pack"] == {"project": "ALPHA", "ticket": "ALPHA-620", "items": []}
