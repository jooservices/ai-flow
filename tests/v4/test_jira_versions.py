from __future__ import annotations

import json

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


def test_jira_reads_edit_transition_and_comment_permissions_without_writing():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"permissions": {
            "EDIT_ISSUES": {"havePermission": False},
            "TRANSITION_ISSUES": {"havePermission": True},
            "ADD_COMMENTS": {"havePermission": False},
        }})

    jira = Jira("https://jira.example.test", "person@example.test", "token",
                client=httpx.Client(transport=httpx.MockTransport(respond), base_url="https://jira.example.test"))

    assert jira.project_write_permissions("TESTWP") == {
        "EDIT_ISSUES": False, "TRANSITION_ISSUES": True, "ADD_COMMENTS": False,
    }
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/rest/api/3/mypermissions"
    assert requests[0].url.params["projectKey"] == "TESTWP"
    assert set(requests[0].url.params["permissions"].split(",")) == {
        "EDIT_ISSUES", "TRANSITION_ISSUES", "ADD_COMMENTS",
    }


def test_releasing_a_jira_version_sets_released_flag_and_release_date():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204)

    jira = Jira("https://jira.example.test", "person@example.test", "token",
                client=httpx.Client(transport=httpx.MockTransport(respond), base_url="https://jira.example.test"))

    jira.release_version("100")

    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/rest/api/3/version/100"
    assert requests[0].read()
    import json
    body = json.loads(requests[0].content)
    assert body["released"] is True and body["releaseDate"]


def test_release_tracker_can_store_the_jira_version_field_on_creation():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/rest/api/3/field":
            return httpx.Response(200, json=[{"id": "customfield_12345", "name": "Version", "custom": True}])
        if request.url.path == "/rest/api/3/issue":
            return httpx.Response(201, json={"key": "TESTWP-900"})
        raise AssertionError(request.url.path)

    jira = Jira("https://jira.example.test", "person@example.test", "token",
                client=httpx.Client(transport=httpx.MockTransport(respond), base_url="https://jira.example.test"))

    key = jira.create_issue("TESTWP", "Release 1.2.0", "Release tracker", issuetype="Release",
                            version_field="1.2.0")

    assert key == "TESTWP-900"
    payload = json.loads(requests[-1].content)
    assert payload["fields"]["customfield_12345"] == "1.2.0"


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
