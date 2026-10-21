"""Project close, report delivery, study-case search and reopen behavior."""
from datetime import datetime, timezone
import uuid

from conftest import add_ticket


def test_manual_close_saves_and_emails_a_searchable_versioned_report(client, ctx, monkeypatch):
    from aiflow.v4.control import notify
    created = datetime(2025, 4, 1, tzinfo=timezone.utc)
    done = datetime(2025, 4, 3, tzinfo=timezone.utc)
    add_ticket(ctx.db, "TESTWP-601", status="Done", labels=())
    ctx.db.run("UPDATE tickets SET jira_created=%s, jira_updated=%s, done_at=%s WHERE key='TESTWP-601'",
               (created, done, done))
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,"
               "started_at,ended_at,tokens_in,tokens_out,cost_usd)"
               " VALUES (%s,'TESTWP-601','TESTWP','verify','codex-01','codex','gpt-6-luna','node1',1,'finished',"
               "'rework',%s,%s,12,8,0.125)", (str(uuid.uuid4()), created, done))
    ctx.db.run("INSERT INTO incidents (severity,kind,subject,title,opened_at)"
               " VALUES ('warning','ticket_stuck','TESTWP-601','Repeated failed run',%s)", (done,))
    sent = []
    monkeypatch.setattr(notify, "send", lambda severity, title, detail="", link="":
                        sent.append((severity, title, detail, link)) or ["email"])

    response = client.post("/v1/projects/TESTWP/close", json={"actor": "lead"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "closed" and data["version"] == 1 and data["created"] is True
    assert data["study_case_ref"] == "TESTWP:v1" and data["sent_channels"] == ["email"]
    report = data["report"]
    assert report["period"]["days"] == 2.0
    assert report["usage"]["tokens_total"] == 20 and report["usage"]["cost_usd"] == 0.125
    assert report["rework_rounds"]["count"] == 1 and report["incidents_opened"]["count"] == 1
    assert report["lessons"] and "rework" in report["lessons"][0]
    assert "## Lessons" in data["markdown"] and "## Tokens and cost" in data["markdown"]
    assert sent[0][0] == "info" and "TESTWP" in sent[0][1] and data["markdown"] in sent[0][2]
    assert sent[0][3] == "/v1/projects/TESTWP/study-cases"

    state = client.get("/v1/state").json()
    project = next(row for row in state["projects"] if row["key"] == "TESTWP")
    assert project["closed"] and project["report_version"] == 1 and not project["close_suggested"]
    cases = client.get("/v1/projects/TESTWP/study-cases").json()["study_cases"]
    assert len(cases) == 1 and cases[0]["ref"] == "TESTWP:v1" and cases[0]["text"] == data["markdown"]
    hits = client.get("/v1/memory/search", params={"project": "TESTWP", "q": "rework rounds"}).json()["hits"]
    assert any(hit["kind"] == "study_case" and hit["ref"] == "TESTWP:v1" for hit in hits)

    duplicate = client.post("/v1/projects/TESTWP/close", json={"actor": "lead"}).json()
    assert duplicate["version"] == 1 and duplicate["created"] is False
    assert len(sent) == 1
    assert ctx.db.one("SELECT count(*) AS n FROM project_reports WHERE scope='project'")["n"] == 1


def test_state_suggests_close_only_when_every_project_ticket_is_done(client, ctx):
    add_ticket(ctx.db, "TESTWP-610", status="Done", labels=())
    project = next(row for row in client.get("/v1/state").json()["projects"] if row["key"] == "TESTWP")
    assert project["status"] == "open" and project["close_suggested"] is True

    add_ticket(ctx.db, "TESTWP-611", status="To Do", labels=())
    project = next(row for row in client.get("/v1/state").json()["projects"] if row["key"] == "TESTWP")
    assert project["close_suggested"] is False


def test_done_epic_generates_an_epic_report_and_email_once(ctx, monkeypatch):
    from aiflow.v4.control import notify
    from aiflow.v4.control.jira import Jira
    from aiflow.v4.control.loops import jira_sync

    add_ticket(ctx.db, "TESTWP-700", status="To Do", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-700'")
    add_ticket(ctx.db, "TESTWP-701", status="Done", labels=())
    ctx.db.run("UPDATE tickets SET parent_key='TESTWP-700' WHERE key='TESTWP-701'")
    issue = {"key": "TESTWP-700", "fields": {
        "summary": "Completed epic", "status": {"name": "Done"}, "issuetype": {"name": "Epic"},
        "created": "2025-04-01T12:00:00.000+0000", "updated": "2025-04-03T12:00:00.000+0000",
        "resolutiondate": "2025-04-03T12:00:00.000+0000",
    }}
    ctx.jira.search = lambda _jql: [issue]
    ctx.jira.normalize = Jira.normalize
    sent = []
    monkeypatch.setattr(notify, "send", lambda severity, title, detail="", link="":
                        sent.append((severity, title, detail)) or ["email"])

    assert jira_sync(ctx, full=True) == 1
    assert jira_sync(ctx, full=True) == 1

    row = ctx.db.one("SELECT scope, subject, version, report, markdown, sent_channels FROM project_reports")
    assert (row["scope"], row["subject"], row["version"]) == ("epic", "TESTWP-700", 1)
    assert row["report"]["epic"] == "TESTWP-700" and row["report"]["tickets"]["done"] == 1
    assert "Done: 1 of 1" in row["markdown"] and row["sent_channels"] == ["email"]
    assert len(sent) == 1 and "epic TESTWP-700" in sent[0][1]
    cases = ctx.db.all("SELECT ref FROM memory WHERE kind='study_case'")
    assert cases == [{"ref": "TESTWP:epic:TESTWP-700:v1"}]


def test_new_synced_ticket_reopens_project_and_next_close_creates_next_version(client, ctx, monkeypatch):
    from aiflow.v4.control import notify
    from aiflow.v4.control.jira import Jira
    from aiflow.v4.control.loops import jira_sync

    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: ["email"])
    first = client.post("/v1/projects/TESTWP/close", json={"actor": "owner"})
    assert first.status_code == 200 and first.json()["version"] == 1

    issue = {"key": "TESTWP-800", "fields": {
        "summary": "New work", "status": {"name": "To Do"}, "issuetype": {"name": "Story"},
        "created": "2025-04-04T12:00:00.000+0000", "updated": "2025-04-04T12:00:00.000+0000",
    }}
    ctx.jira.search = lambda _jql: [issue]
    ctx.jira.normalize = Jira.normalize
    assert jira_sync(ctx, full=True) == 1
    row = ctx.db.one("SELECT status, version FROM project_lifecycle WHERE project='TESTWP'")
    assert row == {"status": "open", "version": 1}
    assert client.get("/v1/state").json()["projects"][0]["close_suggested"] is False

    second = client.post("/v1/projects/TESTWP/close", json={"actor": "owner"})
    assert second.status_code == 200 and second.json()["version"] == 2
    cases = client.get("/v1/projects/TESTWP/study-cases").json()["study_cases"]
    assert {row["ref"] for row in cases} == {"TESTWP:v1", "TESTWP:v2"}
    assert ctx.db.one("SELECT count(*) AS n FROM project_reports WHERE scope='project'")["n"] == 2

