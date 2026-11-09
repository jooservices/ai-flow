"""Memory context ids round-trip into durable usage and effectiveness reports."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from psycopg.errors import ForeignKeyViolation

from aiflow.v4.control import knowledge, notify, run_inbox as run_inbox_control
from aiflow.v4.control.features.runs.bodies import ResultBody
from aiflow.v4.control.features.version_release import claim_steps as version_release_claim
from aiflow.v4.control.repositories import knowledge_usefulness
from aiflow.v4.worker import intake, master
from aiflow.v4.worker.run_steps import (record_reported_knowledge_ids, reported_knowledge_ids,
                                        run_result_metadata, with_knowledge_context)


def test_prompt_round_trips_only_reported_ids_from_the_delivered_pack():
    pack = {"items": [{"id": 17, "kind": "guide", "title": "Release", "content": "Check tags."}]}
    prompt = with_knowledge_context("Do the task.", pack)

    assert "ID 17" in prompt
    assert "KNOWLEDGE_USED_IDS" in prompt
    assert reported_knowledge_ids("KNOWLEDGE_USED_IDS: [17, 999, 17]", pack) == [17]
    assert reported_knowledge_ids("KNOWLEDGE_USED_IDS: []", pack) == []


def test_non_ai_version_release_does_not_record_a_knowledge_delivery():
    project = SimpleNamespace(key="ALPHA", repo="org/alpha", base="main", release_check=[], checks=[],
                              environment_items=[],
                              settings_for_repo=lambda _repo: {}, work_minutes=lambda: {})
    started = SimpleNamespace(key="ALPHA-REL", run_id=str(uuid.uuid4()),
                               lease=SimpleNamespace(fence=3), release_branch="release/1",
                               source_branch="main", source_sha="abc", tracker="1.0")

    claim = version_release_claim.claim_response(
        SimpleNamespace(settings=SimpleNamespace(lease_ms=1000)), None, project,
        {"id": "1", "name": "1.0"}, {"tickets": []},
        SimpleNamespace(id="codex-01", provider="codex"), started, lambda: "bot")

    assert claim["context_pack"]["items"] == []


def test_usefulness_report_ignores_legacy_version_release_packs(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    item = knowledge.save_item(db, project="ALPHA", kind="guide", title="Release guide",
                               content="No AI reads this.", approved=True, approved_by="Admin")
    run_id = str(uuid.uuid4())
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status) "
           "VALUES (%s,'ALPHA-REL','ALPHA','version_release','codex-01','node1',1,'running')", (run_id,))
    knowledge_usefulness.record_context_pack(db, run_id=run_id, project="ALPHA", ticket_key="ALPHA-REL",
                                             epic="", items=[{"id": item["id"]}], token_count=20)

    report = knowledge_usefulness.report(db, project="ALPHA")
    row = next(row for row in report["items"] if row["id"] == item["id"])

    assert row["delivered_count"] == 0
    assert report["pack_tokens"] == {"packs": 0, "known_packs": 0, "tokens": 0}


def test_prompt_round_trip_accepts_the_master_json_result_shape():
    pack = {"items": [{"id": 23, "kind": "convention", "title": "Safe", "content": "Check."}]}

    assert reported_knowledge_ids('{"summary":"ok","knowledge_used_ids":[23]}', pack) == [23]


def test_run_metadata_transmits_reported_context_ids():
    response = SimpleNamespace(tokens_in=12, tokens_out=4, cost_usd=0.1, quota={}, quota_observed_at=None,
                               error_kind="", reset_at=None, final_text="KNOWLEDGE_USED_IDS: [17]")
    pack = {"items": [{"id": 17}]}

    assert run_result_metadata(response, pack)["knowledge_used_ids"] == [17]
    result = {}
    record_reported_knowledge_ids(result, response.final_text, pack)
    assert result == {"knowledge_used_ids": [17]}


def test_intake_provider_result_transmits_reported_context_ids():
    response = SimpleNamespace(tokens_in=1, tokens_out=2, cost_usd=0.0, quota={}, quota_observed_at=None,
                               error_kind="", reset_at=None, exit_code=0, duration_s=1, log_path="",
                               final_text="KNOWLEDGE_USED_IDS: [17]")

    assert intake._provider_result(response, {"items": [{"id": 17}]})["knowledge_used_ids"] == [17]


def test_master_json_result_transmits_reported_context_ids():
    response = SimpleNamespace(tokens_in=1, tokens_out=2, cost_usd=0.0, error_kind="", reset_at=None,
                               final_text='{"summary":"ok","actions":[],"knowledge_used_ids":[17]}')
    result = {}

    master.record_master_usage(result, [response], response, {}, {"items": [{"id": 17}]})

    assert result["knowledge_used_ids"] == [17]


def test_context_pack_token_estimate_is_stable_for_ids_and_text():
    items = [{"id": 8, "title": "Guide", "content": "Check the release tag."}]

    assert knowledge.context_pack_token_count(items) == 9
    assert knowledge.context_pack_token_count([]) == 0


def test_weekly_report_renders_kind_rates_items_and_retirement_review(monkeypatch):
    report = {"by_kind": [{"kind": "guide", "delivered_count": 12, "used_count": 1,
                           "comparison": {"with_item": {"rework_rate": 0.25, "first_pass_rate": 0.5},
                                          "without_item": {"rework_rate": None, "first_pass_rate": 0.0}}}],
              "items": [{"id": 7, "kind": "guide", "title": "Release", "delivered_count": 12,
                         "used_count": 1, "comparison": {"with_item": {"rework_rate": 0.25,
                                                                            "first_pass_rate": 0.5},
                                                     "without_item": {"rework_rate": None,
                                                                          "first_pass_rate": 0.0}}}],
              "retirement_review": [{"id": 8, "title": "Old guide"}]}
    monkeypatch.setattr(knowledge, "usefulness_report", lambda *_args, **_kwargs: report)
    ctx = SimpleNamespace(db=object())
    start = datetime(2026, 11, 2, tzinfo=timezone.utc)

    text = notify._knowledge_usefulness_summary(ctx, start, datetime(2026, 11, 9, tzinfo=timezone.utc))

    assert "guide: delivered 12, used 1" in text and "25.0%/unavailable" in text
    assert "ID 7 guide · Release" in text and "Retirement review: ID 8 Old guide" in text
    assert notify._usefulness_rate(None) == "unavailable" and notify._usefulness_rate(0.125) == "12.5%"


def test_weekly_report_uses_lifetime_retirement_candidates(monkeypatch):
    weekly = {"by_kind": [], "items": [], "retirement_review": []}
    lifetime = {"retirement_review": [{"id": 9, "title": "Old guide"}]}
    calls = []

    def report(_db, *, project=None, start=None, end=None):
        calls.append((project, start, end))
        return weekly if start is not None else lifetime

    monkeypatch.setattr(knowledge, "usefulness_report", report)
    start = datetime(2026, 11, 2, tzinfo=timezone.utc)
    end = datetime(2026, 11, 9, tzinfo=timezone.utc)

    text = notify._knowledge_usefulness_summary(SimpleNamespace(db=object()), start, end)

    assert "Retirement review: ID 9 Old guide" in text
    assert calls == [(None, start, end), (None, None, None)]


def test_context_pack_records_delivered_ids_and_estimated_tokens(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    item = knowledge.save_item(db, project="ALPHA", kind="guide", title="Release guide",
                               content="Check the release tag.", approved=True, approved_by="Admin")
    run_id = str(uuid.uuid4())

    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-1", run_id=run_id)
    stored = db.one("SELECT item_count,token_count FROM knowledge_context_packs WHERE run_id=%s", (run_id,))

    assert {row["item_id"] for row in db.all("SELECT item_id FROM knowledge_deliveries WHERE run_id=%s", (run_id,))} == {
        item["id"]}
    assert stored["item_count"] == 1 and stored["token_count"] == knowledge.context_pack_token_count(pack["items"])


def test_context_pack_delivery_write_rolls_back_as_a_unit(db):
    item = knowledge.save_item(db, project="ALPHA", kind="guide", title="Atomic guide",
                               content="A saved item.", approved=True, approved_by="Admin")
    run_id = str(uuid.uuid4())

    with pytest.raises(ForeignKeyViolation):
        knowledge_usefulness.record_context_pack(
            db, run_id=run_id, project="ALPHA", ticket_key="ALPHA-ATOMIC", epic="",
            items=[{"id": item["id"]}, {"id": 999999}], token_count=20)

    assert db.one("SELECT run_id FROM knowledge_context_packs WHERE run_id=%s", (run_id,)) is None
    assert db.all("SELECT item_id FROM knowledge_deliveries WHERE run_id=%s", (run_id,)) == []
    stored = db.one("SELECT handed_count FROM knowledge_items WHERE id=%s", (item["id"],))
    assert stored["handed_count"] == 0


def test_fenced_run_result_atomically_records_usage_outcome_and_rework_count(ctx, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    item = knowledge.save_item(ctx.db, project="ALPHA", kind="guide", title="Run result guide",
                               content="Use the release checklist.", approved=True, approved_by="Admin")
    run_id = str(uuid.uuid4())
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status) "
               "VALUES (%s,'ALPHA-RESULT','ALPHA','verify','codex-01','node1',4,'running')", (run_id,))
    knowledge.context_pack(ctx.db, project="ALPHA", ticket="ALPHA-RESULT", run_id=run_id)
    run = ctx.db.one("SELECT * FROM runs WHERE id=%s", (run_id,))
    body = ResultBody(fence=4, outcome="rework", knowledge_used_ids=[item["id"], 999999])

    assert run_inbox_control.finish(ctx, run, run_id, body, {}, "node1") is False
    stored = ctx.db.one("SELECT outcome,details FROM runs WHERE id=%s", (run_id,))
    used = ctx.db.all("SELECT item_id FROM knowledge_uses WHERE run_id=%s", (run_id,))

    assert stored["outcome"] == "rework"
    assert stored["details"]["knowledge_used_ids"] == [item["id"]]
    assert stored["details"]["rework_count"] == 1
    assert [row["item_id"] for row in used] == [item["id"]]


def test_usefulness_report_counts_usage_rates_and_retirement_candidates(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    unused = knowledge.save_item(db, project="ALPHA", kind="guide", title="Unused guidance",
                                 content="A guidance item never reported as used.", approved=True, approved_by="Admin")
    used = knowledge.save_item(db, project="ALPHA", kind="convention", title="Used convention",
                               content="A convention that helps.", approved=True, approved_by="Admin")
    for index in range(10):
        run_id = str(uuid.uuid4())
        outcome = "rework" if index in (0, 1, 2) else "passed"
        details = {"verdict": "PASS" if index in (3, 4, 5, 6) else "FAIL",
                   "qc_passed": index in (3, 4, 5, 6)}
        db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,details,ended_at) "
               "VALUES (%s,%s,'ALPHA','verify','codex-01','node1',1,'finished',%s,%s::jsonb,now())",
               (run_id, f"ALPHA-{index}", outcome, json.dumps(details)))
        knowledge_usefulness.record_context_pack(
            db, run_id=run_id, project="ALPHA", ticket_key=f"ALPHA-{index}", epic="",
            items=[{"id": unused["id"]}, {"id": used["id"]}], token_count=30)
        if index in (3, 4):
            with db.transaction() as connection:
                knowledge_usefulness.record_run_usefulness(connection, run_id=run_id,
                                                           ticket_key=f"ALPHA-{index}",
                                                           reported_ids=[used["id"]])

    for index in range(10, 18):
        outcome = "rework" if index in (10, 11) else "passed"
        details = {"verdict": "PASS" if index in (12, 13) else "FAIL", "qc_passed": index in (12, 13)}
        db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,details,ended_at) "
               "VALUES (%s,%s,'ALPHA','verify','codex-01','node1',1,'finished',%s,%s::jsonb,now())",
               (str(uuid.uuid4()), f"ALPHA-{index}", outcome, json.dumps(details)))

    report = knowledge_usefulness.report(db, project="ALPHA")
    unused_row = next(row for row in report["items"] if row["id"] == unused["id"])
    used_row = next(row for row in report["items"] if row["id"] == used["id"])
    guide_kind = next(row for row in report["by_kind"] if row["kind"] == "guide")

    assert unused_row["delivered_count"] == 10 and unused_row["used_count"] == 0
    assert used_row["delivered_count"] == 10 and used_row["used_count"] == 2
    assert unused_row["comparison"]["with_item"]["rework_rate"] == 0.3
    assert unused_row["comparison"]["without_item"]["rework_rate"] == 0.25
    assert unused_row["comparison"]["with_item"]["first_pass_rate"] == 0.4
    assert guide_kind["delivered_count"] == 10 and guide_kind["used_count"] == 0
    assert guide_kind["comparison"]["with_item"]["rework_rate"] == 0.3
    assert guide_kind["comparison"]["without_item"]["first_pass_rate"] == 0.25
    assert report["pack_tokens"] == {"packs": 10, "known_packs": 10, "tokens": 300}
    assert [row["id"] for row in report["retirement_review"]] == [unused["id"]]


def test_first_pass_rate_excludes_a_pass_after_rework(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    item = knowledge.save_item(db, project="ALPHA", kind="guide", title="Retry guide",
                               content="Use the guide.", approved=True, approved_by="Admin")
    attempts = [("ALPHA-RETRY", "rework", {"verdict": "PASS", "qc_passed": True, "rework_count": 1},
                 "2 minutes"),
                ("ALPHA-RETRY", "passed", {"verdict": "PASS", "qc_passed": True, "rework_count": 1},
                 "1 minute")]
    for ticket, outcome, details, offset in attempts:
        run_id = str(uuid.uuid4())
        db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,details,started_at) "
               "VALUES (%s,%s,'ALPHA','verify','codex-01','node1',1,'finished',%s,%s::jsonb,now()-%s::interval)",
               (run_id, ticket, outcome, json.dumps(details), offset))
        knowledge_usefulness.record_context_pack(db, run_id=run_id, project="ALPHA", ticket_key=ticket,
                                                 epic="", items=[{"id": item["id"]}], token_count=20)

    report = knowledge_usefulness.report(db, project="ALPHA")
    row = next(row for row in report["items"] if row["id"] == item["id"])

    assert row["comparison"]["with_item"]["verify_runs"] == 2
    assert row["comparison"]["with_item"]["first_pass_runs"] == 0
    assert row["comparison"]["with_item"]["first_pass_rate"] == 0
