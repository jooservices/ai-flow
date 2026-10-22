from __future__ import annotations

import httpx

from aiflow.v4.control.jira import FIELDS, Jira
from aiflow.v4.control.loops import jira_sync


def jira_issue(key: str, status: str, versions: tuple[str, ...] = ()) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": f"Summary of {key}",
            "status": {"name": status},
            "issuetype": {"name": "Story"},
            "fixVersions": [{"id": version} for version in versions],
        },
    }


def test_jira_reads_project_versions_and_normalizes_fix_version_ids():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=[{"id": "100", "name": "1.0", "released": True,
                                          "releaseDate": "2026-10-01"}])

    jira = Jira("https://jira.example.test", "person@example.test", "token",
                client=httpx.Client(transport=httpx.MockTransport(respond), base_url="https://jira.example.test"))

    assert "fixVersions" in FIELDS
    assert jira.project_versions("TESTWP") == [
        {"id": "100", "name": "1.0", "released": True, "release_date": "2026-10-01"}
    ]
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/rest/api/3/project/TESTWP/versions"
    assert Jira.normalize(jira_issue("TESTWP-1", "To Do", ("100", "200")))["fix_versions"] == ["100", "200"]


def test_jira_sync_upserts_versions_and_replaces_ticket_version_links(ctx):
    ctx.jira.versions_data["TESTWP"] = [
        {"id": "100", "name": "1.0", "released": False, "release_date": None},
        {"id": "200", "name": "2.0", "released": False, "release_date": "2026-12-01"},
    ]
    ctx.jira.search_data = [jira_issue("TESTWP-1", "To Do", ("100",))]

    assert jira_sync(ctx, full=True) == 1
    assert ctx.db.one("SELECT project, name, released, release_date FROM versions WHERE id='100'") == {
        "project": "TESTWP", "name": "1.0", "released": False, "release_date": None,
    }
    assert ctx.db.all("SELECT version_id FROM ticket_versions WHERE ticket_key='TESTWP-1'") == [
        {"version_id": "100"}
    ]

    # Jira includes the changed issue in the incremental updated window after PO adds another Fix version.
    ctx.jira.search_data = [jira_issue("TESTWP-1", "To Do", ("100", "200"))]
    assert jira_sync(ctx) == 1
    assert "updated >= -5m" in ctx.jira.search_queries[-1]
    assert {row["version_id"] for row in ctx.db.all(
        "SELECT version_id FROM ticket_versions WHERE ticket_key='TESTWP-1'")} == {"100", "200"}

    # The next incremental issue row has an empty Fix versions field, so its prior links are removed.
    ctx.jira.search_data = [jira_issue("TESTWP-1", "To Do")]
    assert jira_sync(ctx) == 1
    assert ctx.db.all("SELECT version_id FROM ticket_versions WHERE ticket_key='TESTWP-1'") == []
    assert ctx.jira.version_reads == ["TESTWP", "TESTWP", "TESTWP"]


def test_jira_sync_refreshes_version_metadata_on_the_next_pass(ctx):
    ctx.jira.versions_data["TESTWP"] = [
        {"id": "100", "name": "1.0-beta", "released": False, "release_date": None},
    ]
    ctx.jira.search_data = []
    assert jira_sync(ctx, full=True) == 0

    ctx.jira.versions_data["TESTWP"] = [
        {"id": "100", "name": "1.0", "released": True, "release_date": "2026-10-01"},
    ]
    assert jira_sync(ctx) == 0
    version = ctx.db.one("SELECT project, name, released, release_date FROM versions WHERE id='100'")
    assert (version["project"], version["name"], version["released"]) == ("TESTWP", "1.0", True)
    assert version["release_date"].isoformat() == "2026-10-01"


def test_project_versions_api_reports_done_open_and_unversioned_ticket_counts(client, ctx):
    ctx.jira.versions_data["TESTWP"] = [
        {"id": "100", "name": "1.0", "released": True, "release_date": "2026-10-01"},
        {"id": "200", "name": "2.0", "released": False, "release_date": None},
    ]
    ctx.jira.search_data = [
        jira_issue("TESTWP-1", "Done", ("100",)),
        jira_issue("TESTWP-2", "To Do", ("100",)),
        jira_issue("TESTWP-3", "In Progress", ("100",)),
        jira_issue("TESTWP-4", "To Do"),  # no Fix versions: must not affect either project version
    ]
    assert jira_sync(ctx, full=True) == 4

    response = client.get("/v1/projects/TESTWP/versions")
    assert response.status_code == 200
    versions = response.json()["versions"]
    assert versions == [
        {"id": "100", "name": "1.0", "released": True, "release_date": "2026-10-01",
         "ticket_count": 3, "done_count": 1,
         "open_tickets": [
             {"key": "TESTWP-2", "summary": "Summary of TESTWP-2", "status": "To Do"},
             {"key": "TESTWP-3", "summary": "Summary of TESTWP-3", "status": "In Progress"},
         ]},
        {"id": "200", "name": "2.0", "released": False, "release_date": None,
         "ticket_count": 0, "done_count": 0, "open_tickets": []},
    ]
    assert ctx.db.all("SELECT version_id FROM ticket_versions WHERE ticket_key='TESTWP-4'") == []
    assert client.get("/v1/projects/UNKNOWN/versions").status_code == 404
