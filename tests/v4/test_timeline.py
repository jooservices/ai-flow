"""Ticket timeline: a ticket history built in the test database gives the expected segments and totals."""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from conftest import add_ticket
from aiflow.v4.control.api import create_app
from aiflow.v4.control.db import J
from aiflow.v4.control.jira import Jira
from aiflow.v4.control.loops import jira_sync, watchdog
from aiflow.v4.control.timeline import timeline
from aiflow.v4.control.ui import mount_ui

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def at(s: int) -> datetime:
    return T0 + timedelta(seconds=s)


def event(db, key, type_, s, outcome, **data):
    db.run("INSERT INTO events (ts, type, ticket, outcome, data) VALUES (%s,%s,%s,%s,%s)", (at(s), type_, key, outcome, J(data)))


def run(db, key, step, start, end, outcome, account="claude-01", status="finished"):
    db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, node, fence, status, outcome, started_at,"
           " heartbeat_at, ended_at) VALUES (%s,%s,'TESTWP',%s,%s,'cc',1,%s,%s,%s,%s,%s)",
           (str(uuid.uuid4()), key, step, account, status, outcome, at(start), at(end if end is not None else start),
            at(end) if end is not None else None))


def history(db, key="TESTWP-40"):
    """Seen, made ready, dev, verify sends it back, rework, verify passes to Human review, a person moves it to Done."""
    add_ticket(db, key, status="Done")
    event(db, key, "ticket.status", 0, "To Do", ready=False)
    event(db, key, "ticket.status", 60, "To Do", ready=True)
    run(db, key, "dev", 160, 760, "done")
    event(db, key, "jira.transition", 170, "In Progress")
    event(db, key, "jira.transition", 700, "In Verify")
    run(db, key, "verify", 1000, 1300, "rework", account="codex-01")
    event(db, key, "jira.transition", 1290, "To Do")
    run(db, key, "dev", 1500, 1900, "done")
    event(db, key, "jira.transition", 1510, "In Progress")
    event(db, key, "jira.transition", 1800, "In Verify")
    run(db, key, "verify", 2000, 2200, "human", account="codex-01")
    event(db, key, "jira.transition", 2190, "Human review")
    event(db, key, "ticket.status", 3000, "Done", ready=True)
    return key


def test_segments_cover_queue_runs_rework_and_people(db):
    tl = timeline(db, history(db))
    got = [(s["kind"], s["start"], s["seconds"]) for s in tl["segments"]]
    assert got == [
        ("queued", at(60).isoformat(), 100),     # ready until the first claim
        ("dev", at(160).isoformat(), 600),
        ("queued", at(760).isoformat(), 240),    # In Verify until a verifier claims it
        ("verify", at(1000).isoformat(), 300),
        ("queued", at(1300).isoformat(), 200),   # back in To Do after the rework verdict
        ("rework", at(1500).isoformat(), 400),
        ("queued", at(1900).isoformat(), 100),
        ("verify", at(2000).isoformat(), 200),
        ("waiting", at(2200).isoformat(), 800),  # Human review until a person moved it to Done
    ]
    assert tl["segments"][5]["round"] == 1 and tl["segments"][8]["status"] == "Human review"
    assert all(s["end"] for s in tl["segments"])


def test_totals_for_a_done_ticket(db):
    tt = timeline(db, history(db))["totals"]
    assert tt["lead_time_s"] == 2940 and tt["done"] and tt["done_at"] == at(3000).isoformat()
    assert tt["started_at"] == at(60).isoformat()
    assert tt["ai_s"] == 1500 and tt["queue_s"] == 640 and tt["waiting_s"] == 800
    assert tt["ai_s"] + tt["queue_s"] + tt["waiting_s"] == tt["lead_time_s"]
    assert tt["rework_rounds"] == 1 and tt["runs"] == 4
    assert tt["runs_by_outcome"] == {"done": 2, "rework": 1, "human": 1}


def test_open_ticket_runs_until_now_and_needs_owner_waits(db):
    key = "TESTWP-41"
    add_ticket(db, key, status="In Progress")
    event(db, key, "ticket.status", 0, "To Do", ready=True)
    run(db, key, "dev", 100, 400, "blocked")
    event(db, key, "jira.transition", 110, "In Progress")
    event(db, key, "jira.transition", 390, "Needs owner")
    event(db, key, "ticket.status", 1000, "To Do", ready=True)  # the owner answered
    run(db, key, "dev", 1200, None, "", status="running")
    tl = timeline(db, key, now=at(1500))
    assert [(s["kind"], s["seconds"]) for s in tl["segments"]] == [
        ("queued", 100), ("dev", 300), ("waiting", 600), ("queued", 200), ("dev", 300)]
    tt = tl["totals"]
    assert tt["lead_time_s"] == 1500 and not tt["done"] and tt["done_at"] is None
    assert tt["ai_s"] == 600 and tt["queue_s"] == 300 and tt["waiting_s"] == 600 and tt["rework_rounds"] == 0
    assert tt["runs_by_outcome"] == {"blocked": 1, "running": 1}


def test_orphan_requeue_is_recorded_in_timeline(ctx):
    key = "TESTWP-43"
    add_ticket(ctx.db, key)
    ctx.events.emit("ticket.status", ticket=key, outcome="To Do", ready=True)
    ctx.events.emit("jira.transition", ticket=key, outcome="In Progress")
    ctx.db.run("UPDATE tickets SET status='In Progress', synced_at=now() WHERE key=%s", (key,))
    ctx.jira.status_of[key] = "In Progress"

    watchdog(ctx)

    requeue = ctx.db.one("SELECT ts FROM events WHERE ticket=%s AND type='jira.transition' AND outcome='To Do'",
                         (key,))
    assert requeue is not None
    tl = timeline(ctx.db, key, now=requeue["ts"] + timedelta(seconds=120))
    assert tl["segments"][-1]["kind"] == "queued"
    assert tl["segments"][-1]["start"] == requeue["ts"].isoformat()
    assert tl["segments"][-1]["seconds"] == 120


def test_delayed_done_sync_uses_jira_resolution_time_for_lead_time(ctx):
    key = "TESTWP-44"
    observed = datetime.now(timezone.utc)
    ready_at = observed - timedelta(hours=1)
    completed = observed - timedelta(minutes=20)
    add_ticket(ctx.db, key)
    ctx.db.run("INSERT INTO events (ts, type, ticket, outcome, data) VALUES (%s,'ticket.status',%s,'To Do',%s)",
               (ready_at, key, J({"ready": True, "from_status": ""})))
    issue = {"key": key, "fields": {
        "status": {"name": "Done"}, "labels": ["aiflow-ready"], "updated": observed.isoformat(),
        "resolutiondate": completed.isoformat(),
    }}
    ctx.jira.search = lambda _jql: [issue]
    ctx.jira.normalize = Jira.normalize

    assert jira_sync(ctx, full=True) == 1

    done_event = ctx.db.one("SELECT ts FROM events WHERE ticket=%s AND type='ticket.status' AND outcome='Done'", (key,))
    assert done_event["ts"] > completed  # Jira sync observed completion after Jira resolved the ticket.
    tl = timeline(ctx.db, key)
    assert tl["totals"]["done_at"] == completed.isoformat()
    assert tl["totals"]["lead_time_s"] == int((completed - ready_at).total_seconds())
    assert tl["segments"] == [{
        "kind": "queued", "start": ready_at.isoformat(), "end": completed.isoformat(),
        "status": "To Do", "seconds": int((completed - ready_at).total_seconds()),
    }]


def test_timeline_endpoint_and_ticket_drawer(ctx):
    app = create_app(ctx)
    mount_ui(app, ctx)
    client = TestClient(app)
    key = history(ctx.db)
    r = client.get(f"/v1/tickets/{key}/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"] == key and body["totals"]["lead_time_s"] == 2940 and len(body["segments"]) == 9
    assert client.get("/v1/tickets/TESTWP-404/timeline").status_code == 404
    assert client.get(f"/v1/tickets/{key}").json()["timeline"]["totals"]["queue_s"] == 640


def test_jira_sync_records_status_and_ready_changes(ctx):
    issue = {"key": "TESTWP-42", "fields": {"status": {"name": "To Do"}, "labels": [], "summary": "s"}}
    ctx.jira.search = lambda jql: [issue]
    ctx.jira.normalize = Jira.normalize
    jira_sync(ctx)
    jira_sync(ctx)  # nothing changed: no new event
    issue["fields"]["labels"] = ["aiflow-ready"]
    jira_sync(ctx)
    issue["fields"]["status"] = {"name": "Done"}
    jira_sync(ctx)
    rows = ctx.db.all("SELECT outcome, data FROM events WHERE ticket='TESTWP-42' AND type='ticket.status' ORDER BY id")
    assert [(r["outcome"], r["data"]["ready"], r["data"]["from_status"]) for r in rows] == [
        ("To Do", False, ""), ("To Do", True, "To Do"), ("Done", True, "To Do")]
    tt = timeline(ctx.db, "TESTWP-42")["totals"]
    assert tt["done"] and tt["queue_s"] >= 0
