"""Automatic epic completion, reopening and project-close suggestions from Jira sync."""
import re

from conftest import add_ticket
from aiflow.v4.control.jira import Jira
from aiflow.v4.control.loops import jira_sync


def issue(key, status, kind="Task", parent=None):
    fields = {"summary": f"Summary of {key}", "status": {"name": status},
              "issuetype": {"name": kind}, "labels": []}
    if parent:
        fields["parent"] = {"key": parent}
    return {"key": key, "fields": fields}


def sync_issues(ctx, issues):
    ctx.jira.search = lambda _jql: issues
    ctx.jira.normalize = Jira.normalize


def test_jira_sync_closes_epic_when_every_child_is_done(ctx, monkeypatch):
    from aiflow.v4.control import notify

    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: [])
    epic_key = "TESTWP-100"
    ctx.jira.status_of[epic_key] = "In Progress"
    sync_issues(ctx, [issue(epic_key, "In Progress", "Epic"),
                      issue("TESTWP-101", "Done", parent=epic_key),
                      issue("TESTWP-102", "Done", parent=epic_key)])

    assert jira_sync(ctx, full=True) == 3

    assert ctx.jira.status(epic_key) == "Done"
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (epic_key,))["status"] == "Done"
    audit = ctx.db.one("SELECT result, payload FROM write_audit WHERE ticket=%s AND action='transition'",
                       (epic_key,))
    assert audit["result"] == "Done"
    assert audit["payload"]["child_count"] == 2
    assert audit["payload"]["transition_guard"] == "all_children_finished"
    report = ctx.db.one("SELECT scope, subject, report FROM project_reports WHERE project=%s", ("TESTWP",))
    assert report["scope"] == "epic" and report["subject"] == epic_key
    assert report["report"]["tickets"]["done"] == 2


def test_jira_sync_leaves_an_empty_epic_open(ctx):
    epic_key = "TESTWP-105"
    ctx.jira.status_of[epic_key] = "In Progress"
    sync_issues(ctx, [issue(epic_key, "In Progress", "Epic")])

    jira_sync(ctx, full=True)

    assert ctx.jira.status(epic_key) == "In Progress"
    assert not ctx.jira.comments


def test_jira_sync_reopens_done_epic_when_a_new_child_is_not_done(ctx):
    epic_key = "TESTWP-110"
    add_ticket(ctx.db, epic_key, status="Done", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key=%s", (epic_key,))
    ctx.jira.status_of[epic_key] = "Done"
    sync_issues(ctx, [issue("TESTWP-111", "To Do", parent=epic_key)])

    assert jira_sync(ctx) == 1

    assert ctx.jira.status(epic_key) == "To Do"
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (epic_key,))["status"] == "To Do"
    assert not ctx.jira.comments


def test_first_sync_does_not_reopen_done_epic_for_its_first_seen_unfinished_child(ctx):
    epic_key = "TESTWP-112"
    ctx.jira.status_of[epic_key] = "Done"
    sync_issues(ctx, [issue(epic_key, "Done", "Epic"), issue("TESTWP-113", "To Do", parent=epic_key)])

    assert jira_sync(ctx, full=True) == 2

    assert ctx.jira.status(epic_key) == "Done"
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (epic_key,))["status"] == "Done"
    assert not ctx.db.one("SELECT 1 FROM write_audit WHERE ticket=%s AND action='transition'", (epic_key,))


def test_jira_sync_leaves_done_epic_alone_for_an_old_unfinished_child(ctx):
    epic_key = "TESTWP-115"
    add_ticket(ctx.db, epic_key, status="Done", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key=%s", (epic_key,))
    add_ticket(ctx.db, "TESTWP-116", status="To Do", labels=())
    ctx.db.run("UPDATE tickets SET parent_key=%s WHERE key=%s", (epic_key, "TESTWP-116"))
    ctx.jira.status_of[epic_key] = "Done"
    sync_issues(ctx, [issue(epic_key, "Done", "Epic"), issue("TESTWP-116", "To Do", parent=epic_key)])

    assert jira_sync(ctx, full=True) == 2

    assert ctx.jira.status(epic_key) == "Done"
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (epic_key,))["status"] == "Done"
    assert not ctx.jira.comments


def test_jira_sync_reopens_done_epic_when_an_existing_child_is_reopened(ctx):
    epic_key = "TESTWP-117"
    child_key = "TESTWP-118"
    add_ticket(ctx.db, epic_key, status="Done", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key=%s", (epic_key,))
    add_ticket(ctx.db, child_key, status="Done", labels=())
    ctx.db.run("UPDATE tickets SET parent_key=%s WHERE key=%s", (epic_key, child_key))
    ctx.jira.status_of[epic_key] = "Done"
    sync_issues(ctx, [issue(child_key, "To Do", parent=epic_key)])

    assert jira_sync(ctx, full=True) == 1

    assert ctx.jira.status(epic_key) == "To Do"
    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (epic_key,))["status"] == "To Do"


def test_jira_sync_reports_epic_when_stale_database_status_catches_up_to_jira(ctx, monkeypatch):
    from aiflow.v4.control import notify

    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: [])
    epic_key = "TESTWP-119"
    child_key = "TESTWP-120"
    add_ticket(ctx.db, epic_key, status="In Progress", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key=%s", (epic_key,))
    add_ticket(ctx.db, child_key, status="Done", labels=())
    ctx.db.run("UPDATE tickets SET parent_key=%s WHERE key=%s", (epic_key, child_key))
    ctx.jira.status_of[epic_key] = "Done"
    sync_issues(ctx, [issue(child_key, "Done", parent=epic_key)])

    assert jira_sync(ctx, full=True) == 1

    assert ctx.db.one("SELECT status FROM tickets WHERE key=%s", (epic_key,))["status"] == "Done"
    report = ctx.db.one("SELECT scope, subject FROM project_reports WHERE project=%s", ("TESTWP",))
    assert report == {"scope": "epic", "subject": epic_key}


def test_flow_epic_completion_comment_has_child_count_time_and_report_link(ctx):
    epic_key = "TESTWP-120"
    ctx.jira.status_of[epic_key] = "In Progress"
    sync_issues(ctx, [issue(epic_key, "In Progress", "Epic"),
                      issue("TESTWP-121", "Done", parent=epic_key)])

    jira_sync(ctx, full=True)

    assert len(ctx.jira.comments) == 1
    commented_key, comment = ctx.jira.comments[0]
    assert commented_key == epic_key
    assert comment.startswith("AI Flow · epic completed\nChild tickets: 1 (all Done)\n")
    assert re.search(r"Completed at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", comment)
    assert "http://127.0.0.1:18480/v1/projects/TESTWP/report?epic=TESTWP-120&format=html" in comment
    assert "Project suggestion: all 1 epics are Done" in comment


def test_project_report_suggests_closing_only_when_every_project_epic_is_done(client, ctx):
    add_ticket(ctx.db, "TESTWP-130", status="Done", labels=())
    add_ticket(ctx.db, "TESTWP-131", status="In Progress", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key IN (%s,%s)", ("TESTWP-130", "TESTWP-131"))

    report = client.get("/v1/projects/TESTWP/report").json()
    assert report["project_close_suggestion"] == {"suggested": False, "epics_done": 1, "epics_total": 2}

    ctx.db.run("UPDATE tickets SET status='Done' WHERE key='TESTWP-131'")
    report = client.get("/v1/projects/TESTWP/report").json()
    assert report["project_close_suggestion"] == {"suggested": True, "epics_done": 2, "epics_total": 2}
    markdown = client.get("/v1/projects/TESTWP/report", params={"format": "md"}).text
    html = client.get("/v1/projects/TESTWP/report", params={"format": "html"}).text
    assert "AI Flow suggests considering project closure" in markdown
    assert "AI Flow suggests considering project closure" in html

    epic_markdown = client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-130", "format": "md"})
    epic_html = client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-130", "format": "html"})
    assert epic_markdown.status_code == epic_html.status_code == 200
    assert "AI Flow suggests considering project closure" in epic_markdown.text
    assert "AI Flow suggests considering project closure" in epic_html.text

    closed = client.post("/v1/projects/TESTWP/close", json={"actor": "lead"})
    assert closed.status_code == 200
    assert closed.json()["report"]["project_close_suggestion"] == {
        "suggested": True, "epics_done": 2, "epics_total": 2,
    }
