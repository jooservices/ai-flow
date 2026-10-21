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


def test_jira_sync_closes_epic_when_every_child_is_done(ctx):
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
