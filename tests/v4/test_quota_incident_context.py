"""Quota incident study cases remain stored but stay out of targeted claim knowledge packs."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from aiflow.v4.control import knowledge
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.features.work import claim_extras

_SESSION_SECRET = "quota-incident-context-test-session-secret"
_QUOTA_KINDS = ("account_quota", "quota_high", "quota_prediction")


def _item(db, *, title, content, kind="study_case", source_type="event", source_ref="",
          project="ALPHA", scope="project", epic="", approved=True, status="active"):
    return knowledge.save_item(db, project=project, scope=scope, epic=epic, kind=kind, title=title,
                               content=content, source_type=source_type, source_ref=source_ref,
                               approved=approved, approved_by="AI Flow", status=status)


def _incident(db, kind, incident_id=None):
    if incident_id is None:
        return db.one("INSERT INTO incidents (kind,subject,title) VALUES (%s,%s,%s) RETURNING id",
                      (kind, f"test-{uuid.uuid4()}", "Test incident"))["id"]
    db.run("INSERT INTO incidents (id,kind,subject,title) VALUES (%s,%s,%s,%s)",
           (incident_id, kind, f"test-{incident_id}", "Test incident"))
    return incident_id


def _run(db, step):
    run_id = str(uuid.uuid4())
    db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status) "
           "VALUES (%s,'ALPHA-624','ALPHA',%s,'codex-01','node1',1,'running')", (run_id, step))
    return run_id


def _claim_context(db, step, run_id):
    return SimpleNamespace(body=SimpleNamespace(step=step), ctx=SimpleNamespace(db=db),
                           ticket={"key": "ALPHA-624", "project": "ALPHA", "summary": "reference validation",
                                   "description": "Reference validation."},
                           epic="", run_id=run_id)


def _group_ids(pack):
    return {item["id"] for name, rows in pack.items()
            if name != "items" and isinstance(rows, list) for item in rows}


def _delivery_ids(db, run_id):
    return {row["item_id"] for row in db.all("SELECT item_id FROM knowledge_deliveries WHERE run_id=%s", (run_id,))}


@pytest.mark.parametrize(("step", "exclude"), [("dev", True), ("verify", True), ("qa", True), ("triage", False)])
def test_target_steps_exclude_quota_incidents_and_record_only_delivered_items(db, monkeypatch, step, exclude):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    run_id = _run(db, step)
    excluded = []
    for kind in _QUOTA_KINDS:
        incident_id = _incident(db, kind)
        excluded.append(_item(db, title=f"Reference validation {kind}", content="Reference validation quota case.",
                              source_type="incident", source_ref=str(incident_id)))
    nonquota_id = _incident(db, "node_down")
    nonquota = _item(db, title="Reference validation service case",
                     content="Reference validation after service outage.",
                     source_type="incident", source_ref=str(nonquota_id))
    incident_guide = _item(db, title="Reference validation incident guide", content="Use reference validation safely.",
                           kind="guide", source_type="incident", source_ref="not-a-study-case")
    other_source = _item(db, title="Reference validation event case", content="Reference validation event lesson.")
    completed_ticket = _item(db, title="Reference validation ticket case",
                              content="Reference validation ticket lesson.",
                             source_type="ticket", source_ref="ALPHA-624")

    extra = {}
    claim_extras.context_pack(_claim_context(db, step, run_id), extra)
    pack = extra["context_pack"]
    item_ids, group_ids, delivery_ids = ({item["id"] for item in pack["items"]}, _group_ids(pack),
                                         _delivery_ids(db, run_id))
    excluded_ids = {item["id"] for item in excluded}

    assert (not excluded_ids & item_ids) is exclude
    assert (not excluded_ids & group_ids) is exclude
    assert (not excluded_ids & delivery_ids) is exclude
    assert {nonquota["id"], incident_guide["id"], other_source["id"]} <= item_ids
    assert (completed_ticket["id"] not in item_ids) is exclude
    assert delivery_ids == item_ids
    handed = {row["id"]: row["handed_count"] for row in db.all(
        "SELECT id,handed_count FROM knowledge_items WHERE id=ANY(%s)",
        ([item["id"] for item in excluded] + [nonquota["id"], incident_guide["id"], other_source["id"],
         completed_ticket["id"]],))}
    assert all(handed[item["id"]] == (0 if exclude else 1) for item in excluded)
    assert all(handed[item_id] == 1 for item_id in (nonquota["id"], incident_guide["id"], other_source["id"]))


def test_invalid_and_boundary_incident_references_are_withheld_without_cast_errors(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    quota_id = _incident(db, "quota_high")
    nonquota_id = _incident(db, "node_down")
    short_zero_id = _incident(db, "node_down")
    long_zero_id = _incident(db, "node_down")
    max_id = _incident(db, "node_down", 9223372036854775807)
    eligible = _item(db, title="Reference resolution validation control",
                     content="Reference resolution validation lesson.",
                     source_type="incident", source_ref=str(nonquota_id))
    short_zero = _item(db, title="Reference resolution validation short zero reference",
                       content="Reference resolution validation short zero lesson.",
                       source_type="incident", source_ref=f"000{short_zero_id}")
    long_zero = _item(db, title="Reference resolution validation long zero reference",
                      content="Reference resolution validation long zero lesson.",
                      source_type="incident", source_ref=f"{'0' * 300}{long_zero_id}")
    eligible_max = _item(db, title="Reference resolution validation bigint maximum",
                         content="Reference resolution validation maximum lesson.",
                         source_type="incident", source_ref=str(max_id))
    values = ["", "nondigit", f"+{quota_id}", f" {quota_id}", f"{quota_id} ", "9876543210",
              "9223372036854775808", "9" * 400, "0" * 300 + str(quota_id), "0" * 400]
    excluded = [_item(db, title=f"Reference resolution validation {index}",
                      content="Reference resolution validation quota lesson.", source_type="incident", source_ref=value)
               for index, value in enumerate(values)]

    filtered = knowledge.search_items(db, project="ALPHA", q="reference resolution validation", limit=100,
                                      approved_only=True, kinds=("study_case",), exclude_quota_incidents=True)
    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-624", query="reference resolution validation",
                                  exclude_quota_incidents=True)

    filtered_ids = {item["id"] for item in filtered}
    eligible_ids = {eligible["id"], short_zero["id"], long_zero["id"], eligible_max["id"]}
    assert eligible_ids <= filtered_ids
    assert not {item["id"] for item in excluded} & filtered_ids
    assert eligible_ids <= {item["id"] for item in pack["items"]}
    assert not {item["id"] for item in excluded} & {item["id"] for item in pack["items"]}
    default_hits = knowledge.search_items(db, project="ALPHA", q="reference resolution validation", limit=100,
                                          approved_only=True, kinds=("study_case",))
    assert {item["id"] for item in excluded} <= {item["id"] for item in default_hits}


def test_knowledge_failure_during_claim_keeps_registered_run_and_lease_fence(client, ctx, monkeypatch):
    from aiflow.v4.control.features.work import claim as claim_module
    from aiflow.v4.control.features.work import claim_steps
    from aiflow.v4.control.features.work_routes.bodies import ClaimBody

    ticket = {"key": f"TESTWP-QUOTA-{uuid.uuid4().hex[:8]}", "project": "TESTWP", "step": "dev",
              "summary": "Claim survives unavailable knowledge", "description": "Acceptance criteria",
              "labels": ["aiflow-ready"], "priority": "Medium", "type": "Task", "probation": False}
    deps = SimpleNamespace(eligible=lambda *_args, **_kwargs: [ticket], master_candidate=lambda *_args: ticket,
                           releases=lambda: None, ticket_epic=lambda _ticket: "", capacity_history=lambda: [],
                           related_hits=lambda _ticket: [], bot_identity=lambda: ("AI Flow", "aiflow[bot]"))
    body = ClaimBody(ticket=ticket["key"], account="claude-01", step="dev")

    monkeypatch.setattr(claim_steps.load_policy_api, "work_claim_capacity",
                        lambda *_args: ({"max_node_runs": 10, "hostname": "", "max_host_runs": None}, None))
    monkeypatch.setattr(claim_steps.licensing, "enforce_work_entitlement", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(claim_steps.licensing, "enforce_project_entitlement", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claim_steps.rollout, "node_compatible", lambda *_args: {"compatible": True, "reason": ""})
    monkeypatch.setattr(claim_steps.triage_claim_service, "development_eligible", lambda *_args: True)
    monkeypatch.setattr(claim_steps.triage_claim_service, "authorize_development", lambda *_args: (True, None))
    monkeypatch.setattr(claim_steps.hotfiles, "predict", lambda *_args, **_kwargs: {"hot": []})
    monkeypatch.setattr(claim_steps.hotfiles, "reserve", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claim_steps.capacity, "estimate", lambda *_args: {
        "tokens": {"low": 10, "high": 20}, "time_minutes": {"low": 1, "high": 2}, "size": "S"})
    monkeypatch.setattr(claim_steps.capacity, "with_active_quota_reservations", lambda _db, values: values)
    monkeypatch.setattr(claim_steps.capacity, "account_fit", lambda *_args: {
        "known": False, "fits": False, "left_tokens": None, "reason": "unknown"})
    monkeypatch.setattr(claim_extras.tooling, "resolve_ticket", lambda *_args: None)
    monkeypatch.setattr(claim_extras.tooling, "machine_tools", lambda _row: [])

    def fail_search(*_args, **_kwargs):
        raise RuntimeError("local search unavailable")

    monkeypatch.setattr(knowledge, "context_pack", fail_search)
    monkeypatch.setattr(claim_extras.claim_project, "payload", lambda *_args: {"repo": "test"})
    monkeypatch.setattr(claim_steps, "publish_for", lambda *_args: None)
    monkeypatch.setattr(ctx.provider_eligibility, "record_assignment", lambda *_args: None)

    response = claim_module.claim(ctx, deps, body, "cc")

    assert response["claimed"] is True
    assert response["context_pack"] == {"project": "TESTWP", "ticket": ticket["key"], "items": []}
    run = ctx.db.one("SELECT id,ticket_key,step,fence,status FROM runs WHERE id=%s", (response["run_id"],))
    assert run and str(run["id"]) == response["run_id"]
    assert run["ticket_key"] == ticket["key"] and run["step"] == "dev" and run["status"] == "running"
    assert run["fence"] == response["fence"]
    assert ctx.leases.valid(ticket["key"], response["run_id"], response["fence"])
    assert ctx.leases.holder(ticket["key"]) == f"{response['run_id']}:{response['fence']}"
    assert _delivery_ids(ctx.db, response["run_id"]) == set()


@pytest.mark.parametrize("vector_search", [False, True], ids=["lexical", "vector"])
def test_quota_cases_do_not_consume_eight_search_hits(db, monkeypatch, vector_search):
    vector = "[" + ",".join(["1"] + ["0"] * 383) + "]"
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: vector if vector_search else None)
    eligible = _item(db, title="Copper reference validation case", content="Copper reference validation lesson.",
                     source_ref="older-copper")
    db.run("UPDATE knowledge_items SET created_at=now()-interval '1 day' WHERE id=%s", (eligible["id"],))
    quota_items = []
    for index in range(12):
        incident_id = _incident(db, "quota_high")
        quota_items.append(_item(db, title=f"Copper reference validation quota {index}",
                                 content="Copper copper reference validation copper reference validation quota incident lesson.",
                                 source_type="incident", source_ref=str(incident_id)))
    old_hits = knowledge.search_items(db, project="ALPHA", q="copper reference validation", limit=8,
                                      approved_only=True, kinds=("study_case",))
    filtered = knowledge.search_items(db, project="ALPHA", q="copper reference validation", limit=8,
                                      approved_only=True, kinds=("study_case",), exclude_quota_incidents=True)

    assert len(old_hits) == 8
    assert {item["id"] for item in quota_items} >= {item["id"] for item in old_hits}
    assert eligible["id"] in {item["id"] for item in filtered}
    assert not {item["id"] for item in quota_items} & {item["id"] for item in filtered}
    empty_query = knowledge.search_items(db, project="ALPHA", q="", limit=100, approved_only=True,
                                         kinds=("study_case",), exclude_quota_incidents=True)
    assert eligible["id"] in {item["id"] for item in empty_query}
    assert not {item["id"] for item in quota_items} & {item["id"] for item in empty_query}


def test_quota_exclusions_happen_before_the_500_candidate_limit(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    convention = _item(db, title="Older eligible convention", content="Keep reference validation explicit.",
                       kind="convention", source_ref="older-convention")
    db.run("UPDATE knowledge_items SET created_at=now()-interval '1 day' WHERE id=%s", (convention["id"],))
    incident_rows = db.all("INSERT INTO incidents (kind,subject,title) "
                           "SELECT 'quota_high','candidate-'||n,'Candidate incident' FROM generate_series(1,501) AS n "
                           "RETURNING id")
    incident_ids = [row["id"] for row in incident_rows]
    db.run("INSERT INTO knowledge_items (project,scope,kind,title,content,approved,source_type,source_ref) "
           "SELECT 'ALPHA','project','study_case','Completed quota candidate '||id,"
           "'reference validation quota candidate','true','incident',id::text FROM unnest(%s::bigint[]) AS rows(id)",
           (incident_ids,))
    db.run("UPDATE knowledge_items SET created_at=now()+id*interval '1 second' WHERE source_type='incident' "
           "AND source_ref=ANY(%s)", ([str(item_id) for item_id in incident_ids],))

    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-624", query="reference validation",
                                  exclude_quota_incidents=True)

    assert convention["id"] in {item["id"] for item in pack["items"]}
    assert not any(item["source_type"] == "incident" for item in pack["items"])



def test_scope_approval_and_status_rules_still_apply_with_quota_filter(db, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    quota_id = _incident(db, "quota_high")
    quota = _item(db, title="Checkout context quota lesson", content="Checkout context quota lesson.",
                  source_type="incident", source_ref=str(quota_id))
    project_case = _item(db, title="Checkout context project lesson", content="Checkout context project lesson.")
    general_case = _item(db, title="Checkout context general lesson", content="Checkout context general lesson.",
                         project="", scope="general")
    epic_case = _item(db, title="Checkout context epic lesson", content="Checkout context epic lesson.",
                      epic="ALPHA-EPIC")
    other_epic = _item(db, title="Checkout context other-epic lesson", content="Checkout context other-epic lesson.",
                       epic="ALPHA-OTHER")
    unapproved = _item(db, title="Checkout context pending lesson", content="Checkout context pending lesson.",
                       approved=False)
    retired = _item(db, title="Checkout context retired lesson", content="Checkout context retired lesson.",
                    status="retired")

    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-624", epic="ALPHA-EPIC",
                                  query="checkout context lesson", exclude_quota_incidents=True)
    selected = {item["id"] for item in pack["items"]}

    assert {project_case["id"], general_case["id"], epic_case["id"]} <= selected
    assert not {quota["id"], other_epic["id"], unapproved["id"], retired["id"]} & selected


def test_default_search_and_authorized_knowledge_api_keep_quota_cases_visible(client, ctx, monkeypatch):
    monkeypatch.setattr(knowledge, "_embedding", lambda _text: None)
    incident_id = _incident(ctx.db, "account_quota")
    quota_case = _item(ctx.db, title="Visible quota reference validation case",
                       content="Quota reference validation remains stored.", source_type="incident",
                       source_ref=str(incident_id))
    ctx.settings.console_session_secret = _SESSION_SECRET
    denied = client.get("/v1/knowledge/items",
                        params={"project": "ALPHA", "q": "quota reference validation"})
    assert denied.status_code == 401
    client.cookies.set("aiflow4_session", issue_session("owner@example.test", "owner", 2_000_000_000,
                                                         _SESSION_SECRET))

    response = client.get("/v1/knowledge/items", params={"project": "ALPHA", "q": "quota reference validation"})
    default_hits = knowledge.search_items(ctx.db, project="ALPHA", q="quota reference validation",
                                         approved_only=True, kinds=("study_case",))

    assert response.status_code == 200
    assert quota_case["id"] in {item["id"] for item in response.json()["items"]}
    assert quota_case["id"] in {item["id"] for item in default_hits}


def test_empty_pack_and_search_failure_do_not_record_false_deliveries(db, monkeypatch):
    quota_id = _incident(db, "quota_prediction")
    _item(db, title="Only excluded quota reference", content="An only reference validation quota lesson.",
          source_type="incident", source_ref=str(quota_id))
    empty_run = _run(db, "dev")
    pack = knowledge.context_pack(db, project="ALPHA", ticket="ALPHA-624", query="reference validation",
                                  run_id=empty_run, exclude_quota_incidents=True)
    assert pack["items"] == [] and _group_ids(pack) == set()
    assert _delivery_ids(db, empty_run) == set()
    assert db.one("SELECT item_count,token_count FROM knowledge_context_packs WHERE run_id=%s", (empty_run,)) == {
        "item_count": 0, "token_count": 0}

    failed_run = _run(db, "verify")
    def fail_search(*_args, **_kwargs):
        raise RuntimeError("local search unavailable")
    monkeypatch.setattr(knowledge, "context_pack", fail_search)
    extra = {}
    claim_extras.context_pack(_claim_context(db, "verify", failed_run), extra)
    assert extra["context_pack"] == {"project": "ALPHA", "ticket": "ALPHA-624", "items": []}
    assert _delivery_ids(db, failed_run) == set()
