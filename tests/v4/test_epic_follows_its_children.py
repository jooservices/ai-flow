"""Epic state follows child work on direct transitions and watchdog reconciliation (JAIF-440)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from conftest import add_ticket, join_node
from aiflow.v4.control import epic_lifecycle
from aiflow.v4.control.epic_lifecycle import (
    apply_transition,
    children_for,
    is_under_epic,
    reconcile,
    target_status,
    transition_reason,
)


def _ticket(key, status, kind="Story", parent=None):
    return {"key": key, "status": status, "type": kind, "parent_key": parent, "labels": []}


def _epic(ctx, key, status):
    add_ticket(ctx.db, key, status=status, labels=())
    ctx.db.run("UPDATE tickets SET type='Epic', done_at=CASE WHEN %s='Done' THEN now() ELSE done_at END WHERE key=%s",
               (status, key))
    ctx.jira.status_of[key] = status


def _child(ctx, key, parent, status="To Do"):
    add_ticket(ctx.db, key, status=status)
    ctx.db.run("UPDATE tickets SET type='Story', parent_key=%s WHERE key=%s", (parent, key))
    ctx.jira.status_of[key] = status


def _claim(client, key, node_name="cc"):
    joined = join_node(client, node_name).json()
    headers = {"Authorization": f"Bearer {joined['node_token']}"}
    run = client.post("/v1/work/claim", json={"ticket": key, "account": "claude-01"}, headers=headers).json()
    assert run["claimed"]
    return headers, run


def _transition(client, headers, run, key, target):
    return client.post(f"/v1/jira/{key}/transition",
                       json={"run_id": run["run_id"], "fence": run["fence"], "to": target}, headers=headers)


def _deliver_move(ctx, key):
    from aiflow.v4.control.platform.events import wiring

    event = ctx.db.one("SELECT id FROM domain_events WHERE type='ticket.moved' AND aggregate_id=%s"
                       " ORDER BY seq DESC LIMIT 1", (key,))
    assert event
    wiring.dispatcher_for(ctx).run_once(event_id=event["id"])


def test_parent_walk_and_child_selection_include_nested_non_epics():
    epic = _ticket("T-1", "To Do", "Epic")
    direct = _ticket("T-2", "In Progress", parent="T-1")
    nested = _ticket("T-3", "Done", "Sub-task", "T-2")
    nested_epic = _ticket("T-4", "In Progress", "Epic", "T-1")
    by_key = {item["key"]: item for item in (epic, direct, nested, nested_epic)}

    assert is_under_epic(nested, "T-1", by_key)
    assert not is_under_epic(_ticket("T-5", "To Do", parent="missing"), "T-1", by_key)
    assert children_for(epic, list(by_key.values()), by_key) == [direct, nested]


def test_target_status_covers_start_completion_reopen_and_first_sync_guard():
    assert target_status(_ticket("E-1", "To Do", "Epic"), [], set(), set(), set()) is None
    assert target_status(_ticket("E-1", "To Do", "Epic"), [_ticket("C-1", "Done")],
                         set(), set(), set()) == "Done"
    assert target_status(_ticket("E-1", "In Progress", "Epic"),
                         [_ticket("C-1", "Done"), _ticket("C-2", "Declined")],
                         set(), set(), set()) == "Done"
    assert target_status(_ticket("E-1", "To Do", "Epic"), [_ticket("C-1", "In Verify")],
                         set(), set(), set()) == "In Progress"
    assert target_status(_ticket("E-1", "To Do", "Epic"), [_ticket("C-1", "In Progress")],
                         set(), {"C-1"}, set()) is None
    assert target_status(_ticket("E-1", "To Do", "Epic"), [_ticket("C-1", "In Progress")],
                         set(), {"C-1"}, {"E-1"}) == "In Progress"
    assert target_status(_ticket("E-1", "Done", "Epic"), [_ticket("C-1", "To Do")],
                         {"C-1"}, set(), {"E-1"}) == "To Do"
    assert target_status(_ticket("E-1", "Done", "Epic"), [_ticket("C-1", "To Do")],
                         {"C-1"}, {"C-1"}, set()) is None
    assert target_status(_ticket("E-1", "In Progress", "Epic"), [_ticket("C-1", "To Do")],
                         set(), set(), set()) is None


def test_watchdog_reopen_requires_a_child_move_after_epic_completion():
    completed_at = datetime.now(timezone.utc)
    epic = {**_ticket("E-1", "Done", "Epic"), "done_at": completed_at}
    child = _ticket("C-1", "To Do")

    assert target_status(epic, [child], set(), set(), set(), {"C-1": completed_at + timedelta(seconds=1)}) == "To Do"
    assert target_status(epic, [child], set(), set(), set(), {"C-1": completed_at - timedelta(seconds=1)}) is None
    assert target_status(epic, [child], {"C-1"}, set(), {"E-1"},
                         {"C-1": completed_at - timedelta(seconds=1)}) is None
    assert target_status(epic, [child], {"C-1"}, {"C-1"}, {"E-1"},
                         {"C-1": completed_at - timedelta(seconds=1)}) == "To Do"


def test_watchdog_retries_a_reparented_child_without_a_status_move():
    completed_at = datetime.now(timezone.utc)
    epic = {**_ticket("E-1", "Done", "Epic"), "done_at": completed_at}
    child = {**_ticket("C-1", "To Do"), "parent_changed_at": completed_at + timedelta(seconds=1)}

    assert target_status(epic, [child], set(), set(), set(), {}) == "To Do"
    child["parent_changed_at"] = completed_at - timedelta(seconds=1)
    assert target_status(epic, [child], set(), set(), set(), {}) is None


def test_transition_reason_preserves_existing_done_wording_and_names_declined_children():
    assert transition_reason("Done", [_ticket("C-1", "Done")]) == "all 1 child tickets are Done"
    assert transition_reason("Done", [_ticket("C-1", "Declined")]) == "all 1 child tickets are Done or Declined"
    assert transition_reason("In Progress", [_ticket("C-1", "In Progress")]) == "the first child ticket started"
    assert transition_reason("To Do", [_ticket("C-1", "To Do")]) == "1 child tickets are not all Done"


def test_wrong_jira_result_leaves_epic_mirror_unchanged_without_writes_or_events():
    writes = []
    emitted = []

    class Jira:
        def status(self, _key):
            return "To Do"

        def transition_to(self, _key, _target):
            return "In Progress"

    epic = _ticket("E-1", "To Do", "Epic")
    repository = SimpleNamespace(update_status_with_done_timestamp=lambda params: writes.append(params),
                                 record_epic_transition=lambda *args: writes.append(args))
    events = SimpleNamespace(emit=lambda *args, **kwargs: emitted.append((args, kwargs)))
    apply_transition(SimpleNamespace(jira=Jira(), ticket_repository=repository, events=events), "TEST", epic,
                     [_ticket("C-1", "Done")],
                     [epic], "Done", "unit")

    assert epic["status"] == "To Do"
    assert writes == [] and emitted == []


def test_transition_rechecks_descendants_after_waiting_for_the_epic_lock():
    rows = [
        {**_ticket("E-1", "In Progress", "Epic"), "project": "TEST", "done_at": None},
        {**_ticket("C-1", "Done", parent="E-1"), "project": "TEST", "done_at": None},
    ]
    transitions = []

    class Repository:
        def lock_epic_lifecycle(self, _key):
            # Model a child reopen that committed while this reconciliation waited for the lock.
            rows[1]["status"] = "To Do"

        def list_project_tickets_for_epic_reconcile(self, _params):
            return [dict(row) for row in rows]

        def latest_project_ticket_moves(self, _project):
            return []

    class Jira:
        def status(self, _key):
            return "In Progress"

        def transition_to(self, key, status):
            transitions.append((key, status))
            return status

    epic = dict(rows[0])
    child = dict(rows[1])
    apply_transition(SimpleNamespace(jira=Jira(), ticket_repository=Repository()), "TEST", epic,
                     [child], [epic], "Done", "unit")

    assert transitions == []
    assert epic["status"] == "In Progress"


def test_reconcile_skips_when_jira_is_not_configured(monkeypatch):
    calls = []
    repository = SimpleNamespace(list_project_tickets_for_epic_reconcile=lambda params: calls.append(params))
    ctx = SimpleNamespace(jira=None, ticket_repository=repository)

    reconcile(ctx, ["TESTWP"], set(), set(), set())

    assert calls == []


def test_epic_completion_followups_survive_independent_failures_and_retry(ctx, monkeypatch, caplog):
    from aiflow.v4.control import merge_flow
    from aiflow.v4.control.features.tickets import ticket_repository_of

    epic_key, child_key = "TESTWP-447", "TESTWP-448"
    _epic(ctx, epic_key, "In Progress")
    _child(ctx, child_key, epic_key, "Done")
    repository = ticket_repository_of(ctx)
    original_emit = ctx.events.emit
    original_report = epic_lifecycle.project_lifecycle.report_done_epic
    original_move = merge_flow.record_move
    original_audit = repository.record_epic_transition
    original_comment = ctx.jira.comment

    def fail(*_args, **_kwargs):
        raise RuntimeError("temporary follow-up failure")

    monkeypatch.setattr(ctx.events, "emit", fail)
    monkeypatch.setattr(epic_lifecycle.project_lifecycle, "report_done_epic", fail)
    monkeypatch.setattr(merge_flow, "record_move", fail)
    monkeypatch.setattr(repository, "record_epic_transition", fail)
    monkeypatch.setattr(ctx.jira, "comment", fail)

    epic = _ticket(epic_key, "In Progress", "Epic")
    child = _ticket(child_key, "Done", parent=epic_key)
    apply_transition(ctx, "TESTWP", epic, [child], [epic], "Done", "unit")

    assert ctx.jira.status(epic_key) == "Done"
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (epic_key,))["status"] == "Done"
    pending = repository.pending_epic_transition_followups()
    assert len(pending) == 1
    assert not all((pending[0].status_event_done, pending[0].report_done, pending[0].transition_event_done,
                    pending[0].move_done, pending[0].audit_done, pending[0].comment_done))

    monkeypatch.setattr(ctx.events, "emit", original_emit)
    monkeypatch.setattr(epic_lifecycle.project_lifecycle, "report_done_epic", original_report)
    monkeypatch.setattr(merge_flow, "record_move", original_move)
    monkeypatch.setattr(repository, "record_epic_transition", original_audit)
    monkeypatch.setattr(ctx.jira, "comment", original_comment)
    epic_lifecycle.retry_epic_transition_followups(ctx)

    assert repository.pending_epic_transition_followups() == []
    assert ctx.db.one("SELECT count(*) AS n FROM project_reports WHERE project=%s AND scope='epic' AND subject=%s",
                      ("TESTWP", epic_key))["n"] == 1
    assert ctx.db.one("SELECT count(*) AS n FROM ticket_moves WHERE ticket=%s AND cause='epic children'",
                      (epic_key,))["n"] == 1
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE ticket=%s AND action='transition'",
                      (epic_key,))["n"] == 1
    assert ctx.jira.comments[-1][0] == epic_key
    assert "could not deliver epic TESTWP-447 follow-up" in caplog.text


def test_accepted_epic_transition_is_reconciled_after_local_persistence_failure(ctx, monkeypatch):
    from aiflow.v4.control.features.tickets import ticket_repository_of

    epic_key, child_key = "TESTWP-454", "TESTWP-455"
    _epic(ctx, epic_key, "In Progress")
    _child(ctx, child_key, epic_key, "Done")
    repository = ticket_repository_of(ctx)
    queue_followup = repository.queue_epic_transition_followup

    def fail_once(values):
        monkeypatch.setattr(repository, "queue_epic_transition_followup", queue_followup)
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(repository, "queue_epic_transition_followup", fail_once)
    epic = _ticket(epic_key, "In Progress", "Epic")
    child = _ticket(child_key, "Done", parent=epic_key)
    apply_transition(ctx, "TESTWP", epic, [child], [epic], "Done", "unit")

    assert ctx.jira.status(epic_key) == "Done"
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (epic_key,))["status"] == "In Progress"

    reconcile(ctx, ["TESTWP"], set(), set(), set(), "unit")

    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (epic_key,))["status"] == "Done"
    assert ctx.db.one("SELECT count(*) AS n FROM ticket_moves WHERE ticket=%s AND cause='epic children'",
                      (epic_key,))["n"] == 1
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE ticket=%s AND action='transition'",
                      (epic_key,))["n"] == 1
    assert ctx.jira.comments[-1][0] == epic_key


def test_epic_start_does_not_override_a_manual_jira_close():
    transitions = []

    class Jira:
        def status(self, _key):
            return "Done"

        def transition_to(self, key, target):
            transitions.append((key, target))
            return target

    epic = _ticket("E-1", "To Do", "Epic")
    ctx = SimpleNamespace(jira=Jira())

    apply_transition(ctx, "TEST", epic, [_ticket("C-1", "In Progress")], [epic], "In Progress", "unit")

    assert transitions == [] and epic["status"] == "To Do"


def test_jira_sync_wrapper_delegates_to_the_lifecycle_module(monkeypatch):
    from aiflow.v4.control import loops

    calls = []
    monkeypatch.setattr(epic_lifecycle, "reconcile", lambda *args: calls.append(args))
    ctx = object()

    loops._reconcile_epics(ctx, ["TESTWP"], {"TESTWP-449"}, {"TESTWP-450"}, {"TESTWP-451"})

    assert calls == [(ctx, ["TESTWP"], {"TESTWP-449"}, {"TESTWP-450"}, {"TESTWP-451"})]


def test_child_start_and_reopen_update_epic_through_the_jira_transition_route(client, ctx):
    _epic(ctx, "TESTWP-440", "To Do")
    _child(ctx, "TESTWP-441", "TESTWP-440")
    headers, run = _claim(client, "TESTWP-441")

    started = _transition(client, headers, run, "TESTWP-441", "In Progress")
    assert started.status_code == 200 and started.json() == {"status": "In Progress"}
    _deliver_move(ctx, "TESTWP-441")
    assert ctx.jira.status("TESTWP-440") == "In Progress"
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-440'")["status"] == "In Progress"

    done = _transition(client, headers, run, "TESTWP-441", "Done")
    assert done.status_code == 200
    _deliver_move(ctx, "TESTWP-441")
    assert ctx.jira.status("TESTWP-440") == "Done"

    reopened = _transition(client, headers, run, "TESTWP-441", "To Do")
    _deliver_move(ctx, "TESTWP-441")
    assert reopened.status_code == 200 and ctx.jira.status("TESTWP-440") == "To Do"
    transitions = ctx.db.all("SELECT result FROM write_audit WHERE ticket='TESTWP-440' AND action='transition' ORDER BY id")
    assert [row["result"] for row in transitions] == ["In Progress", "Done", "To Do"]


def test_epic_finishes_when_children_are_done_or_declined(client, ctx):
    _epic(ctx, "TESTWP-442", "In Progress")
    _child(ctx, "TESTWP-443", "TESTWP-442", "Done")
    _child(ctx, "TESTWP-444", "TESTWP-442")
    headers, run = _claim(client, "TESTWP-444", "cc")

    response = _transition(client, headers, run, "TESTWP-444", "Declined")
    _deliver_move(ctx, "TESTWP-444")

    assert response.status_code == 200 and ctx.jira.status("TESTWP-442") == "Done"
    audit = ctx.db.one("SELECT result,payload FROM write_audit WHERE ticket='TESTWP-442' AND action='transition'")
    assert audit["result"] == "Done"
    assert audit["payload"]["reason"] == "all 2 child tickets are Done or Declined"
    assert "Child tickets: 2 (Done: 1, Declined: 1)" in ctx.jira.comments[-1][1]


def test_watchdog_retries_epic_reopen_after_child_transition_failure(client, ctx, monkeypatch, caplog):
    from aiflow.v4.control import watchdog

    _epic(ctx, "TESTWP-445", "Done")
    _child(ctx, "TESTWP-446", "TESTWP-445")
    headers, run = _claim(client, "TESTWP-446", "cc")
    assert _transition(client, headers, run, "TESTWP-446", "Done").status_code == 200
    real_transition = ctx.jira.transition_to

    def fail_epic_transition(key, target):
        if key == "TESTWP-445":
            raise RuntimeError("Jira temporarily unavailable")
        return real_transition(key, target)

    monkeypatch.setattr(ctx.jira, "transition_to", fail_epic_transition)
    response = _transition(client, headers, run, "TESTWP-446", "To Do")
    assert response.status_code == 200
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-445'")["status"] == "Done"

    monkeypatch.setattr(ctx.jira, "transition_to", real_transition)
    watchdog.run_checks(ctx, (watchdog.CHECKS[-1],))

    assert ctx.jira.status("TESTWP-445") == "To Do"


def test_cross_project_child_prevents_epic_completion(client, ctx):
    from aiflow.v4.control.features.tickets import ticket_repository_of

    _epic(ctx, "TESTWP-447", "In Progress")
    _child(ctx, "TESTWP-448", "TESTWP-447", "Done")
    _child(ctx, "OTHER-449", "TESTWP-447", "To Do")

    rows = ticket_repository_of(ctx).list_project_tickets_for_epic_reconcile(("TESTWP",))
    assert any(row.key == "OTHER-449" and row.project == "OTHER" for row in rows)
    reconcile(ctx, ["TESTWP"], set(), set(), set(), "unit")

    assert ctx.jira.status("TESTWP-447") == "In Progress"


def test_watchdog_retries_reopen_for_reparented_child_after_a_failed_transition(client, ctx, monkeypatch):
    from aiflow.v4.control import watchdog

    _epic(ctx, "TESTWP-450", "Done")
    _child(ctx, "OTHER-451", "TESTWP-450", "To Do")
    changed_at = datetime.now(timezone.utc) + timedelta(seconds=2)
    ctx.db.run("UPDATE tickets SET fields=jsonb_build_object('parent_changed_at',%s::timestamptz) WHERE key=%s",
               (changed_at, "OTHER-451"))
    real_transition = ctx.jira.transition_to

    def fail_once(key, target):
        if key == "TESTWP-450":
            monkeypatch.setattr(ctx.jira, "transition_to", real_transition)
            raise RuntimeError("Jira temporarily unavailable")
        return real_transition(key, target)

    monkeypatch.setattr(ctx.jira, "transition_to", fail_once)
    reconcile(ctx, ["TESTWP"], {"OTHER-451"}, {"OTHER-451"}, {"TESTWP-450"}, "jira_sync", {})
    assert ctx.jira.status("TESTWP-450") == "Done"

    watchdog.run_checks(ctx, (watchdog.CHECKS[-1],))

    assert ctx.jira.status("TESTWP-450") == "To Do"


def test_watchdog_retries_cross_project_child_move_when_child_project_is_not_active(client, ctx, monkeypatch):
    from aiflow.v4.control import watchdog
    from aiflow.v4.control.features.tickets import ticket_repository_of

    _epic(ctx, "TESTWP-452", "Done")
    _child(ctx, "OTHER-453", "TESTWP-452", "To Do")
    changed_at = datetime.now(timezone.utc) + timedelta(seconds=2)
    ctx.db.run("UPDATE tickets SET done_at=%s WHERE key=%s", (changed_at - timedelta(seconds=5), "TESTWP-452"))
    ctx.db.run("INSERT INTO ticket_moves (ts,ticket,from_status,to_status,cause)"
               " VALUES (%s,%s,'Done','To Do','person')", (changed_at, "OTHER-453"))
    moves = ticket_repository_of(ctx).latest_project_ticket_moves("TESTWP")
    assert any(move.ticket == "OTHER-453" and move.ts == changed_at for move in moves)
    assert "OTHER" not in ctx.repo().projects

    real_transition = ctx.jira.transition_to

    def fail_epic_once(key, status):
        if key == "TESTWP-452":
            monkeypatch.setattr(ctx.jira, "transition_to", real_transition)
            raise RuntimeError("Jira temporarily unavailable")
        return real_transition(key, status)

    monkeypatch.setattr(ctx.jira, "transition_to", fail_epic_once)
    reconcile(ctx, ["TESTWP"], set(), set(), set(), "unit")
    assert ctx.jira.status("TESTWP-452") == "Done"
    assert ctx.db.one("SELECT count(*) AS n FROM epic_reopen_requests WHERE epic=%s", ("TESTWP-452",))["n"] == 1
    reopened_moves = {move.ticket: move.ts for move in ticket_repository_of(ctx).latest_project_ticket_moves("TESTWP")}
    epic_row = next(row for row in ticket_repository_of(ctx).list_project_tickets_for_epic_reconcile(("TESTWP",))
                    if row.key == "TESTWP-452")
    assert target_status({**_ticket("TESTWP-452", "Done", "Epic"), "done_at": epic_row.done_at},
                         [_ticket("OTHER-453", "To Do", parent="TESTWP-452")], set(), set(), set(),
                         reopened_moves) == "To Do"

    watchdog.run_checks(ctx, (watchdog.CHECKS[-1],))

    assert ctx.jira.status("TESTWP-452") == "To Do"
