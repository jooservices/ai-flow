"""Characterization and regression tests for the scale-sensitive state and project-list reads."""

from conftest import add_ticket
from aiflow.v4.control import release_progress


def test_state_and_project_picker_keep_their_ticket_and_project_contract(client, ctx):
    add_ticket(ctx.db, "TESTWP-588")

    state_response = client.get("/v1/state")
    picker_response = client.get("/v1/projects")

    assert state_response.status_code == picker_response.status_code == 200
    state = state_response.json()
    assert {"projects", "tickets", "accounts", "nodes", "active_runs", "recent_runs"} <= set(state)
    [ticket] = [row for row in state["tickets"] if row["key"] == "TESTWP-588"]
    assert (ticket["project"], ticket["status"], ticket["summary"], ticket["repository"]) == (
        "TESTWP", "To Do", "Summary of TESTWP-588", "jooservices/test-aiflow-wp")
    project = next(row for row in state["projects"] if row["key"] == "TESTWP")
    assert project["counts"] == {"To Do": 1}

    picker = picker_response.json()
    [listed] = [row for row in picker["projects"] if row["key"] == "TESTWP"]
    assert listed["ticket_count"] == 1
    assert {"repo", "repositories", "status", "priority", "base", "stack", "node_readiness", "services",
            "checks", "check_commands", "pipeline_checks", "pipeline_only"} <= set(listed)
    assert picker["invalid_projects"] == []


def test_project_picker_reads_counts_readiness_and_probes_in_one_round_trip(client, ctx, monkeypatch):
    original_one = ctx.db.one
    original_all = ctx.db.all
    listing_reads = []

    def recording_one(sql, params=None):
        if "ticket_counts" in sql and "project_check_probes" in sql and "readiness" in sql:
            listing_reads.append(sql)
        return original_one(sql, params)

    def recording_all(sql, params=None):
        assert not ("FROM nodes" in sql or "FROM project_check_probes" in sql
                    or "FROM tickets" in sql and "GROUP BY project" in sql)
        return original_all(sql, params)

    monkeypatch.setattr(ctx.db, "one", recording_one)
    monkeypatch.setattr(ctx.db, "all", recording_all)

    assert client.get("/v1/projects").status_code == 200
    assert len(listing_reads) == 1


def test_project_picker_preserves_check_probe_formatting(client, ctx):
    from datetime import datetime, timezone

    from aiflow.v4.control import tooling
    from aiflow.v4.control.db import J

    checked_at = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
    results = [{"status": "cannot_run", "command": "npm test"}, {"status": "ran", "command": "npm ci"}]
    ctx.db.run("INSERT INTO project_check_probes (project,node,signature,checked_at,results)"
               " VALUES (%s,%s,%s,%s,%s)", ("TESTWP", "cc", "old-signature", checked_at, J(results)))
    expected = tooling.probe_report(ctx)["TESTWP"]

    response = client.get("/v1/projects")

    assert response.status_code == 200
    listed = next(row for row in response.json()["projects"] if row["key"] == "TESTWP")
    assert listed["checks"] == expected


def test_release_steps_batch_audits_events_and_run_nodes_for_all_trackers():
    from datetime import datetime, timezone

    class Database:
        def __init__(self):
            self.statements = []

        def all(self, sql, params=None):
            self.statements.append(sql)
            if "FROM write_audit" in sql:
                return [{"ticket": "REL-A", "id": 1, "ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
                         "action": "github-release-files", "run_id": "run-a", "result": "",
                         "payload": {"files": ["CHANGELOG.md"]}}]
            if "FROM events" in sql:
                return [{"ticket": "REL-B", "ts": datetime(2026, 1, 2, tzinfo=timezone.utc),
                         "run_id": "run-b", "outcome": "failed", "reason": "checks failed"}]
            return [{"id": "run-a", "node": "node-a"}, {"id": "run-b", "node": "node-b"}]

    db = Database()
    steps = release_progress._release_steps_many(db, [("REL-A", "running"), ("REL-B", "failed")])

    assert len(db.statements) == 3
    assert steps[("REL-A", "running")][0] == {
        "step": "check", "state": "done", "run_id": "run-a", "node": "node-a",
        "detail": "release checks passed", "at": "2026-01-01T00:00:00+00:00"}
    assert steps[("REL-A", "running")][1]["detail"] == "CHANGELOG.md"
    assert steps[("REL-B", "failed")][0]["state"] == "failed"
    assert steps[("REL-B", "failed")][0]["node"] == "node-b"
