from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiflow.v4.control import version_release
from aiflow.v4.control.db import J
from aiflow.v4.core.controlrepo import Project
from aiflow.v4.worker import version_release as release_worker
from aiflow.v4.worker.dev import Claimed
from conftest import add_ticket, join_node


CORE = "WordPress Core 0.5.0-beta"
THEME = "WordPress Theme 0.5.0-beta"


def issue(key: str, status: str = "Done") -> dict:
    return {"key": key, "fields": {"summary": f"Summary {key}", "status": {"name": status}}}


def configure(ctx, versions=(("101", CORE),), issues=None) -> None:
    project = ctx.repo().projects["TESTWP"]
    project.version_release = {"enabled": True, "versions": [name for _, name in versions],
                               "tag_prefixes": {CORE: "testwp-core", THEME: "testwp-theme"}}
    ctx.jira.version_data["TESTWP"] = [{"id": version_id, "name": name, "released": False, "archived": False}
                                       for version_id, name in versions]
    for version_id, _ in versions:
        ctx.jira.version_issue_data[("TESTWP", version_id)] = list((issues or {}).get(version_id, []))


def evidence(ctx, key: str, *, status: str = "Done", reviewer: bool = True, qa: bool = True,
             qc: bool = True, version_id: str = "101") -> None:
    add_ticket(ctx.db, key, status=status, labels=())
    now = datetime.now(timezone.utc)
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,started_at)"
               " VALUES (%s,%s,'TESTWP','dev','codex-01','codex','node1',1,'finished','done',%s)",
               (str(uuid.uuid4()), key, now - timedelta(minutes=2)))
    if reviewer or not qa or not qc:
        verify_outcome = "merged" if qa else "human"
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
                   "started_at,details) VALUES (%s,%s,'TESTWP','verify','claude-01','claude','cc',1,'finished',%s,%s,%s)",
                   (str(uuid.uuid4()), key, verify_outcome, now - timedelta(minutes=1),
                    J({"verdict": "PASS" if reviewer else "FAIL", "qc_passed": qc})))
    if not qa:
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,started_at)"
                   " VALUES (%s,%s,'TESTWP','qa','reviewer-console','human','control',1,'finished','rework',%s)",
                   (str(uuid.uuid4()), key, now - timedelta(seconds=30)))
    ctx.jira.version_issue_data[("TESTWP", version_id)].append(issue(key, status))


def node_headers(client, name="cc"):
    joined = join_node(client, name).json()
    return {"Authorization": f"Bearer {joined['node_token']}"}


def claim_release(client, headers, version_id="101"):
    response = client.post("/v1/version-releases/claim",
                          json={"project": "TESTWP", "version_id": version_id, "account": "claude-01"},
                          headers=headers)
    return response


def test_version_membership_is_read_live_and_includes_tickets_added_later(ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-1")
    evidence(ctx, "TESTWP-2")

    first = version_release.evaluate(ctx, ctx.repo().projects["TESTWP"], ctx.jira.version_data["TESTWP"][0])
    ctx.jira.version_issue_data[("TESTWP", "101")].append(issue("TESTWP-3", "To Do"))
    second = version_release.evaluate(ctx, ctx.repo().projects["TESTWP"], ctx.jira.version_data["TESTWP"][0])

    assert [t["key"] for t in first["tickets"]] == ["TESTWP-1", "TESTWP-2"]
    assert [t["key"] for t in second["tickets"]] == ["TESTWP-1", "TESTWP-2", "TESTWP-3"]
    assert second["reasons"] == ["TESTWP-3: missing Done, reviewer PASS, QA merge, QC checks"]


def test_release_notes_escape_multiline_jira_summaries_as_markdown_text():
    snapshot = {"version": CORE, "tickets": [{
        "key": "TESTWP-999",
        "summary": "Core update\r\n## Injected\n![image](https://evil.test/x) <img src=x>",
    }]}

    notes = version_release._changelog(snapshot, "a" * 40, [])

    assert ("- [TESTWP-999] Core update \\#\\# Injected \\!\\[image\\]\\(https\\:\\/\\/evil\\.test\\/x\\) "
            "\\<img src\\=x\\>") in notes
    assert "Core update\r" not in notes and "Core update\n" not in notes
    assert "\n## Injected" not in notes and "\n![image]" not in notes


def test_release_check_and_tag_prefix_can_be_set_on_project_configuration(ctx):
    configure(ctx)
    project = ctx.repo().projects["TESTWP"]
    project.release_check = ["vendor/bin/phpunit", "composer validate"]
    project.tag_prefix = "core"
    project.version_release["tag_prefixes"] = {}

    assert version_release.check_commands(project) == ["vendor/bin/phpunit", "composer validate"]
    assert version_release.release_tag(project, CORE) == "core-0.5.0-beta"


@pytest.mark.parametrize(("status", "reviewer", "qa", "qc", "missing"), [
    ("In Progress", True, True, True, "Done"),
    ("Done", False, True, True, "reviewer PASS"),
    ("Done", True, False, True, "QA merge"),
    ("Done", True, True, False, "QC checks"),
])
def test_gate_blocks_and_audits_each_missing_done_reviewer_qa_or_qc(ctx, status, reviewer, qa, qc, missing):
    configure(ctx)
    evidence(ctx, "TESTWP-10", status=status, reviewer=reviewer, qa=qa, qc=qc)

    assert version_release.scan(ctx) == 1

    tracker = ctx.db.one("SELECT tracker_key,status,gate_detail,ticket_snapshot FROM version_releases"
                         " WHERE project='TESTWP' AND version_id='101'")
    assert tracker["status"] == "blocked"
    assert "TESTWP-10" in tracker["gate_detail"] and missing in tracker["gate_detail"]
    assert any(key == tracker["tracker_key"] and "TESTWP-10" in comment and missing in comment
               for key, comment in ctx.jira.comments)
    assert getattr(ctx.github, "merges", []) == []
    assert getattr(ctx.github, "tags", {}) == {}
    assert getattr(ctx.github, "releases", {}) == {}
    assert ctx.jira.released_versions == []


def test_candidate_claim_rechecks_live_gate_before_any_release_step(client, ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-20")
    headers = node_headers(client)
    candidate = client.get("/v1/version-releases/candidates?account=claude-01", headers=headers).json()
    assert len(candidate["versions"]) == 1
    ctx.jira.version_issue_data[("TESTWP", "101")].append(issue("TESTWP-21", "To Do"))

    response = claim_release(client, headers)

    assert response.status_code == 409
    assert "TESTWP-21" in response.text and "Done" in response.text
    tracker = ctx.db.one("SELECT tracker_key,gate_detail FROM version_releases WHERE project='TESTWP' AND version_id='101'")
    assert "TESTWP-21" in tracker["gate_detail"]
    assert any(key == tracker["tracker_key"] and "TESTWP-21" in comment for key, comment in ctx.jira.comments)
    assert getattr(ctx.github, "merges", []) == []
    assert getattr(ctx.github, "tags", {}) == {}
    assert getattr(ctx.github, "releases", {}) == {}


def test_release_claim_is_fenced_and_stale_completion_cannot_publish(client, ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-22")
    headers = node_headers(client)
    claim = claim_release(client, headers).json()
    identity = version_release.version_identity("TESTWP", "101")
    assert ctx.leases.release(identity, claim["run_id"], claim["fence"])
    assert ctx.leases.claim(identity, "replacement-run", ctx.settings.lease_ms).ok

    response = client.post("/v1/version-releases/101/complete",
                           json={"run_id": claim["run_id"], "fence": claim["fence"], "head_sha": "a" * 40,
                                 "checks": [{"command": "composer install", "passed": True}]}, headers=headers)

    assert response.status_code == 409
    assert getattr(ctx.github, "merges", []) == []
    assert getattr(ctx.github, "tags", {}) == {}
    assert getattr(ctx.github, "releases", {}) == {}


def test_full_project_check_worker_uses_current_develop_checkout_and_reports_result(tmp_path, monkeypatch):
    git_refs = []

    class FakeGit:
        def __init__(self, _root, run_as=None):
            self.refs = git_refs

        def mirror(self, _repo, _token):
            return tmp_path

        def remote_branch_exists(self, _mirror, branch):
            return branch == "release/core-a"

        def detached(self, _mirror, path, ref):
            self.refs.append(ref)
            path.mkdir(parents=True, exist_ok=True)
            return "a" * 40

    class FakeApi:
        def __init__(self):
            self.calls = []

        def call(self, method, path, json=None, params=None):
            self.calls.append((method, path, json, params))
            if path == "/v1/github/token":
                return {"token": "test-token"}
            if path.endswith("/complete"):
                return {"released": True, "status": "released", "tag": "testwp-core-0.5.0-beta"}
            raise AssertionError(path)

    monkeypatch.setattr(release_worker, "Git", FakeGit)
    monkeypatch.setattr(release_worker, "project_env", lambda _project: {})
    api = FakeApi()
    claimed = Claimed(run_id="run-1", fence=1, run_token="", account="claude-01", provider="claude",
                      model="", effort="", ticket={"key": "release:TESTWP:1"},
                      project={"key": "TESTWP", "repo": "jooservices/test-aiflow-wp", "checks": ["true"],
                               "source_branch": "develop", "work_minutes": {"hard": 1}},
                      identity=("bot", "bot@example.test"), step="version_release")
    claimed.project.update({"release_branch": "release/core-a", "source_sha": "a" * 40})

    result = release_worker.run_version_release(api, claimed, {"id": "101", "name": CORE}, "TESTWP-900",
                                                str(tmp_path), 1, [])

    assert result["outcome"] == "done"
    assert git_refs == ["origin/release/core-a"]
    complete = next(call for call in api.calls if call[1].endswith("/complete"))
    assert complete[2]["head_sha"] == "a" * 40
    assert complete[2]["source_sha"] == "a" * 40
    assert complete[2]["checks"] == [{"command": "true", "passed": True, "exit_code": 0, "output": ""}]


def test_failed_release_checks_create_one_bug_per_failed_command_linked_as_blocker(ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-30")
    version = ctx.jira.version_data["TESTWP"][0]
    snapshot = version_release.evaluate(ctx, ctx.repo().projects["TESTWP"], version)
    version_release.record_gate(ctx, ctx.repo().projects["TESTWP"], version, snapshot)
    tracker = ctx.db.one("SELECT tracker_key FROM version_releases") ["tracker_key"]
    run = {"id": "run-failed", "fence": 7, "account_id": "claude-01", "node": "cc"}

    result = version_release.complete(ctx, ctx.repo().projects["TESTWP"], version, run,
                                      {"head_sha": "a" * 40, "checks": [
                                          {"command": "composer test", "passed": False, "exit_code": 1,
                                           "output": "assertion failed"},
                                          {"command": "npm test", "passed": False, "exit_code": 2,
                                           "output": "test failed"}]})

    bugs = [item for item in ctx.jira.created if item["issuetype"] == "Bug"]
    assert result["status"] == "failed" and len(bugs) == 2
    assert all({"release-check", "release-qa", "aiflow-ready"} <= set(bug["labels"])
               for bug in bugs)
    assert all(ctx.jira.status_of[bug["key"]] == "Backlog" for bug in bugs)
    assert ctx.jira.issue_links == [(bugs[0]["key"], tracker, "Blocks"), (bugs[1]["key"], tracker, "Blocks")]
    assert ctx.db.one("SELECT status FROM version_releases WHERE project='TESTWP' AND version_id='101'")["status"] == "failed"
    assert getattr(ctx.github, "merges", []) == []
    assert ctx.github.prs == [] and ctx.github.commits == []
    assert getattr(ctx.github, "tags", {}) == {}
    assert getattr(ctx.github, "releases", {}) == {}
    comments_before = len(ctx.jira.comments)

    replay = version_release.complete(ctx, ctx.repo().projects["TESTWP"], version, run,
                                      {"head_sha": "a" * 40, "checks": [
                                          {"command": "composer test", "passed": False, "exit_code": 1,
                                           "output": "assertion failed"},
                                          {"command": "npm test", "passed": False, "exit_code": 2,
                                           "output": "test failed"}]})

    assert replay["bugs"] == [{"key": bug["key"], "command": command}
                              for bug, command in zip(bugs, ("composer test", "npm test"))]
    assert len([item for item in ctx.jira.created if item["issuetype"] == "Bug"]) == 2
    assert len(ctx.jira.comments) == comments_before


def test_failed_bugs_enter_the_live_gate_and_are_rechecked_only_after_done(ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-40")
    version = ctx.jira.version_data["TESTWP"][0]
    snapshot = version_release.evaluate(ctx, ctx.repo().projects["TESTWP"], version)
    version_release.record_gate(ctx, ctx.repo().projects["TESTWP"], version, snapshot)
    version_release.complete(ctx, ctx.repo().projects["TESTWP"], version,
                             {"id": "run-fail", "fence": 1, "account_id": "claude-01", "node": "cc"},
                             {"head_sha": "a" * 40, "checks": [{"command": "composer test", "passed": False,
                                                                    "exit_code": 1, "output": "failed"}]})
    bug = next(row for row in ctx.jira.created if row["issuetype"] == "Bug")
    project = ctx.repo().projects["TESTWP"]
    assert bug["key"] not in {row["key"] for row in ctx.jira.version_issue_data[("TESTWP", "101")]}
    blocked = version_release.evaluate(ctx, project, version)
    assert not blocked["ready"] and f"{bug['key']}: release-check Bug is Backlog, not Done" in blocked["reasons"]
    ctx.jira.status_of[bug["key"]] = "Done"
    ready = version_release.evaluate(ctx, project, version)
    assert ready["ready"]


def test_release_check_bug_done_restarts_release_from_checks_without_jira_version_membership(client, ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-45")
    ctx.github.branch_heads[("test-aiflow-wp", "develop")] = "a" * 40
    project = ctx.repo().projects["TESTWP"]
    version = ctx.jira.version_data["TESTWP"][0]
    version_release.record_gate(ctx, project, version, version_release.evaluate(ctx, project, version))
    version_release.complete(ctx, project, version,
                             {"id": "run-lag", "fence": 1, "account_id": "claude-01", "node": "cc"},
                             {"head_sha": "a" * 40, "checks": [{"command": "composer test", "passed": False,
                                                                "exit_code": 1, "output": "failed"}]})
    bug = next(row for row in ctx.jira.created if row["issuetype"] == "Bug")
    headers = node_headers(client)

    # The release QA Bug stays in Backlog and blocks by status, independent of Jira version indexing.
    lagging = version_release.evaluate(ctx, project, version)
    candidates = client.get("/v1/version-releases/candidates?account=claude-01", headers=headers).json()

    assert not lagging["ready"]
    assert f"{bug['key']}: release-check Bug is Backlog, not Done" in lagging["reasons"]
    assert candidates["versions"] == []
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE step='version_release'")["n"] == 0
    assert len([row for row in ctx.jira.created if row["issuetype"] == "Bug"]) == 1

    ctx.jira.status_of[bug["key"]] = "Done"
    ready = client.get("/v1/version-releases/candidates?account=claude-01", headers=headers).json()
    assert [item["version_id"] for item in ready["versions"]] == ["101"]
    claim = claim_release(client, headers)
    assert claim.status_code == 200, claim.text
    run = claim.json()
    result = client.post("/v1/version-releases/101/complete",
                         json={"run_id": run["run_id"], "fence": run["fence"],
                               "head_sha": run["project"]["source_sha"],
                               "source_sha": run["project"]["source_sha"],
                               "checks": [{"command": "composer install", "passed": True},
                                          {"command": "vendor/bin/phpunit", "passed": True}]}, headers=headers)
    assert result.status_code == 200 and result.json()["released"] is True


def test_publish_error_parks_version_behind_one_follow_up_instead_of_reclaiming(client, ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-46")
    ctx.github.branch_heads[("test-aiflow-wp", "develop")] = "a" * 40
    headers = node_headers(client)
    merge_pr = ctx.github.merge_pr

    def protected(repo, number, title="", method="squash"):
        return {"merged": False, "sha": "", "message": "409 merge conflict: master is protected"}

    ctx.github.merge_pr = protected

    def worker_iteration(expected: str = "failed") -> int:
        claims = 0
        for release in client.get("/v1/version-releases/candidates?account=claude-01",
                                  headers=headers).json()["versions"]:
            claim = claim_release(client, headers, release["version_id"])
            if claim.status_code != 200:
                continue
            claims += 1
            run = claim.json()
            checked_head = ctx.github.branch_sha("test-aiflow-wp", run["project"]["release_branch"])
            result = client.post("/v1/version-releases/101/complete",
                                 json={"run_id": run["run_id"], "fence": run["fence"], "head_sha": checked_head,
                                       "source_sha": run["project"]["source_sha"],
                                       "checks": [{"command": "composer install", "passed": True},
                                                  {"command": "vendor/bin/phpunit", "passed": True}]},
                                 headers=headers)
            assert result.status_code == 200 and result.json()["status"] == expected, result.text
            client.post(f"/v1/runs/{run['run_id']}/release", json={"fence": run["fence"]}, headers=headers)
        return claims

    claims = worker_iteration() + worker_iteration()

    tracker = ctx.db.one("SELECT tracker_key FROM version_releases")["tracker_key"]
    tasks = [row for row in ctx.jira.created if row["issuetype"] == "Task"]
    error_comments = [body for key, body in ctx.jira.comments if key == tracker and "integration error" in body]
    state = ctx.db.one("SELECT status,gate_detail FROM version_releases WHERE project='TESTWP' AND version_id='101'")
    assert claims == 1
    assert len([task for task in tasks if "release-publish" in task["labels"]]) == 1
    assert not [task for task in tasks if "merge-back" in task["labels"]]
    assert ctx.jira.issue_links == [(tasks[0]["key"], tracker, "Blocks")]
    assert len(error_comments) == 1 and tasks[0]["key"] in error_comments[0]
    assert state["status"] == "failed" and "master is protected" in state["gate_detail"]
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='jira-release-publish-failed'")["n"] == 1
    assert ctx.github.tags == {} and ctx.jira.released_versions == []

    ctx.jira.status_of[tasks[0]["key"]] = "Done"
    ctx.github.merge_pr = merge_pr
    assert worker_iteration("released") == 1
    assert ctx.jira.released_versions == ["101"] and len(ctx.github.tags) == 1
    assert len([row for row in ctx.jira.created if row["issuetype"] == "Task"
                and "release-publish" in row["labels"]]) == 1
    assert len([row for row in ctx.jira.created if row["issuetype"] == "Task"
                and "merge-back" in row["labels"]]) == 1


def test_success_releases_version_once_with_ticket_notes_tag_audit_and_merge_back(client, ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-50")
    evidence(ctx, "TESTWP-51")
    ctx.github.branch_heads[("test-aiflow-wp", "develop")] = "a" * 40
    ctx.github.merge_commit_enabled = False
    headers = node_headers(client)
    claim = claim_release(client, headers)
    assert claim.status_code == 200, claim.text
    run = claim.json()

    result = client.post("/v1/version-releases/101/complete",
                         json={"run_id": run["run_id"], "fence": run["fence"], "head_sha": "a" * 40,
                               "source_sha": run["project"]["source_sha"],
                               "checks": [{"command": "composer install", "passed": True, "exit_code": 0},
                                          {"command": "vendor/bin/phpunit", "passed": True, "exit_code": 0}]},
                         headers=headers)

    assert result.status_code == 200 and result.json()["released"] is True
    assert ctx.github.merges == []
    merge_back_tasks = [row for row in ctx.jira.created if row["issuetype"] == "Task"
                        and "merge-back" in row["labels"]]
    assert len(merge_back_tasks) == 1
    assert ctx.github.prs[0]["head"].startswith("release/") and ctx.github.prs[0]["base"] == "master"
    assert ctx.github.prs[0]["draft"] is False
    assert ctx.github.merged == [("test-aiflow-wp", 1, "Release WordPress Core 0.5.0-beta", "merge")]
    assert ctx.github.merge_commit_enable_calls == ["test-aiflow-wp"]
    release_branch = ctx.github.prs[0]["head"]
    assert "TESTWP-50" in ctx.github.files[("test-aiflow-wp", release_branch, "CHANGELOG.md")]
    assert "TESTWP-51" in ctx.github.files[("test-aiflow-wp", release_branch, "CHANGELOG.md")]
    assert "### Added" in ctx.github.files[("test-aiflow-wp", release_branch, "CHANGELOG.md")]
    assert "Current version: `0.5.0-beta`" in ctx.github.files[("test-aiflow-wp", release_branch, "README.md")]
    assert '"version": "0.5.0-beta"' in ctx.github.files[("test-aiflow-wp", release_branch, "package.json")]
    tag = "testwp-core-0.5.0-beta"
    assert ctx.github.tags[("test-aiflow-wp", tag)] == "merged-pr-0001"
    release = ctx.github.releases[("test-aiflow-wp", tag)]
    assert "TESTWP-50" in release["body"] and "TESTWP-51" in release["body"]
    assert "composer install" in release["body"]
    assert ctx.jira.released_versions == ["101"]
    assert ctx.jira.version_release_dates["101"] == datetime.now(timezone.utc).date().isoformat()
    release_tracker = next(row for row in ctx.jira.created if row["issuetype"] == "Release")
    assert release_tracker["version_field"] == CORE
    assert not [transition for transition in ctx.jira.transitions if transition[1] == "Human review"]
    tracker = claim.json()["tracker_key"]
    comment = next(body for key, body in reversed(ctx.jira.comments) if key == tracker and "Version released" in body)
    assert "TESTWP-50" in comment and "a" * 12 in comment and tag in comment and "GitHub Release:" in comment
    state = ctx.db.one("SELECT status,master_sha,tag,github_release_url FROM version_releases"
                       " WHERE project='TESTWP' AND version_id='101'")
    assert state["status"] == "released" and state["master_sha"] == "merged-pr-0001" and state["tag"] == tag
    audit = ctx.db.all("SELECT action,result FROM write_audit WHERE ticket=%s ORDER BY id", (tracker,))
    assert {item["action"] for item in audit} >= {"github-tag", "github-release", "github-merge", "jira-version-released"}
    assert "Version released" in comment and "merge-back ticket" in ctx.jira.comments[-1][1].lower()
    candidates = client.get("/v1/version-releases/candidates?account=claude-01", headers=headers).json()
    assert candidates["versions"] == []
    repeated = claim_release(client, headers)
    assert repeated.status_code == 404
    assert len(ctx.github.tags) == len(ctx.github.releases) == 1
    before_commits = len(ctx.github.commits)
    replay = version_release.complete(ctx, ctx.repo().projects["TESTWP"], ctx.jira.version_data["TESTWP"][0],
                                      {"id": run["run_id"], "fence": run["fence"],
                                       "account_id": "claude-01", "node": "cc"},
                                      {"head_sha": "a" * 40, "source_sha": "a" * 40,
                                       "checks": [{"command": "composer install", "passed": True}]})
    assert replay["released"] is True
    assert len(ctx.github.prs) == 1 and len(ctx.github.tags) == len(ctx.github.releases) == 1
    assert len(ctx.github.commits) == before_commits and ctx.jira.released_versions == ["101"]


def test_scan_repairs_jira_released_version_after_registry_write_failure(client, ctx, monkeypatch):
    configure(ctx)
    evidence(ctx, "TESTWP-55")
    ctx.github.branch_heads[("test-aiflow-wp", "develop")] = "a" * 40
    headers = node_headers(client)
    claim = claim_release(client, headers)
    assert claim.status_code == 200, claim.text
    run = claim.json()
    set_state = version_release._set_state
    failed_writes = 0

    def fail_released_state_twice(context, project_key, version_id, **values):
        nonlocal failed_writes
        if values.get("status") == "released" and failed_writes < 2:
            failed_writes += 1
            raise RuntimeError("temporary registry write failure")
        return set_state(context, project_key, version_id, **values)

    monkeypatch.setattr(version_release, "_set_state", fail_released_state_twice)
    response = client.post("/v1/version-releases/101/complete",
                           json={"run_id": run["run_id"], "fence": run["fence"], "head_sha": "a" * 40,
                                 "checks": [{"command": "composer install", "passed": True},
                                            {"command": "vendor/bin/phpunit", "passed": True}]},
                           headers=headers)

    assert response.status_code == 200 and response.json()["status"] == "reconciling"
    assert ctx.jira.released_versions == ["101"]
    assert ctx.db.one("SELECT status FROM version_releases WHERE project='TESTWP' AND version_id='101'")["status"] == "checking"
    assert len([row for row in ctx.jira.created if row["issuetype"] == "Task"
                and "merge-back" in row["labels"]]) == 1
    assert ctx.github.merges == []
    client.post(f"/v1/runs/{run['run_id']}/release", json={"fence": run["fence"]}, headers=headers)

    monkeypatch.setattr(version_release, "_set_state", set_state)
    assert version_release.scan(ctx) == 1
    state = ctx.db.one("SELECT status,master_sha,tag FROM version_releases"
                       " WHERE project='TESTWP' AND version_id='101'")
    assert state == {"status": "released", "master_sha": "merged-pr-0001", "tag": "testwp-core-0.5.0-beta"}
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='jira-version-released'")["n"] == 1
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='jira-merge-back-task'")["n"] == 1

    version_release.scan(ctx)
    assert ctx.github.merges == []
    assert len(ctx.github.tags) == len(ctx.github.releases) == 1
    assert ctx.jira.released_versions == ["101"]
    assert len([row for row in ctx.jira.created if row["issuetype"] == "Task"
                and "merge-back" in row["labels"]]) == 1


def test_core_and_theme_versions_in_one_repo_get_distinct_tags_and_releases(ctx):
    configure(ctx, versions=(("101", CORE), ("102", THEME)))
    evidence(ctx, "TESTWP-60", version_id="101")
    evidence(ctx, "TESTWP-61", version_id="102")
    project = ctx.repo().projects["TESTWP"]
    for index, version in enumerate(ctx.jira.version_data["TESTWP"]):
        checked_sha = chr(ord("a") + index) * 40
        ctx.github.branch_heads[("test-aiflow-wp", "develop")] = checked_sha
        snapshot = version_release.evaluate(ctx, project, version)
        version_release.record_gate(ctx, project, version, snapshot)
        result = version_release.complete(ctx, project, version,
                                          {"id": f"run-{version['id']}", "fence": 3,
                                           "account_id": "claude-01", "node": "cc"},
                                          {"head_sha": checked_sha,
                                           "checks": [{"command": "composer test", "passed": True}]})
        assert result["released"], result

    assert set(ctx.github.tags) == {("test-aiflow-wp", "testwp-core-0.5.0-beta"),
                                    ("test-aiflow-wp", "testwp-theme-0.5.0-beta")}
    assert set(ctx.github.releases) == set(ctx.github.tags)


def test_failed_merge_back_creates_a_ready_jira_follow_up_task(ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-70")
    ctx.github.branch_heads[("test-aiflow-wp", "develop")] = "a" * 40
    project = ctx.repo().projects["TESTWP"]
    version = ctx.jira.version_data["TESTWP"][0]
    snapshot = version_release.evaluate(ctx, project, version)
    version_release.record_gate(ctx, project, version, snapshot)
    result = version_release.complete(ctx, project, version,
                                      {"id": "run-merge-back", "fence": 4,
                                       "account_id": "claude-01", "node": "cc"},
                                      {"head_sha": "a" * 40,
                                       "checks": [{"command": "composer test", "passed": True}]})

    task = next(row for row in ctx.jira.created if row["issuetype"] == "Task")
    assert result["released"] is True
    assert "merge-back" in task["labels"] and "aiflow-ready" in task["labels"]
    assert ctx.jira.status_of[task["key"]] == "To Do"
    assert ctx.github.merges == []
    assert any(key == ctx.db.one("SELECT tracker_key FROM version_releases")["tracker_key"]
               and task["key"] in comment for key, comment in ctx.jira.comments)


def test_no_merge_back_ticket_is_created_when_master_has_no_missing_commits(ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-71")
    ctx.github.branch_heads[("test-aiflow-wp", "develop")] = "a" * 40
    ctx.github.compare_results[("test-aiflow-wp", "develop", "master")] = {"ahead_by": 0, "status": "identical"}
    project = ctx.repo().projects["TESTWP"]
    version = ctx.jira.version_data["TESTWP"][0]
    version_release.record_gate(ctx, project, version, version_release.evaluate(ctx, project, version))

    result = version_release.complete(ctx, project, version,
                                      {"id": "run-no-back", "fence": 5,
                                       "account_id": "claude-01", "node": "cc"},
                                      {"head_sha": "a" * 40,
                                       "checks": [{"command": "composer test", "passed": True}]})
    version_release.scan(ctx)

    assert result["released"] is True
    assert not [row for row in ctx.jira.created if row["issuetype"] == "Task" and "merge-back" in row["labels"]]
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='github-merge-back'")["n"] == 1
    assert ctx.github.merges == []


def test_completion_for_another_version_than_the_claimed_lease_is_rejected_without_side_effects(client, ctx):
    configure(ctx, versions=(("101", CORE), ("102", THEME)))
    evidence(ctx, "TESTWP-80", version_id="101")
    evidence(ctx, "TESTWP-81", version_id="102")
    ctx.github.branch_heads[("test-aiflow-wp", "develop")] = "a" * 40
    headers = node_headers(client)
    claim = claim_release(client, headers, "101")
    assert claim.status_code == 200, claim.text
    run = claim.json()
    audits_before = ctx.db.one("SELECT count(*) AS n FROM write_audit")["n"]
    comments_before = len(ctx.jira.comments)

    response = client.post("/v1/version-releases/102/complete",
                           json={"run_id": run["run_id"], "fence": run["fence"], "head_sha": "a" * 40,
                                 "checks": [{"command": "composer install", "passed": True},
                                            {"command": "vendor/bin/phpunit", "passed": True}]},
                           headers=headers)

    assert response.status_code == 403
    assert getattr(ctx.github, "merges", []) == []
    assert getattr(ctx.github, "tags", {}) == {}
    assert getattr(ctx.github, "releases", {}) == {}
    assert ctx.jira.released_versions == []
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit")["n"] == audits_before
    assert len(ctx.jira.comments) == comments_before
    assert not [row for row in getattr(ctx.jira, "created", []) if row["issuetype"] in ("Bug", "Task")]


def test_merge_back_runs_even_when_the_released_comment_fails(ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-82")
    ctx.github.branch_heads[("test-aiflow-wp", "develop")] = "a" * 40
    project = ctx.repo().projects["TESTWP"]
    version = ctx.jira.version_data["TESTWP"][0]
    version_release.record_gate(ctx, project, version, version_release.evaluate(ctx, project, version))
    comment = ctx.jira.comment

    def failing_comment(key, text):
        if "Version released" in text:
            raise RuntimeError("Jira 503")
        return comment(key, text)

    ctx.jira.comment = failing_comment
    with pytest.raises(RuntimeError, match="Jira 503"):
        version_release.complete(ctx, project, version,
                                 {"id": "run-comment", "fence": 5, "account_id": "claude-01", "node": "cc"},
                                 {"head_sha": "a" * 40, "checks": [{"command": "composer test", "passed": True}]})

    assert ctx.github.merges == []
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='jira-merge-back-task'")["n"] == 1
    assert ctx.db.one("SELECT status FROM version_releases")["status"] == "released"


def test_scan_retries_merge_back_for_a_released_version_without_a_merge_back_record(ctx):
    configure(ctx)
    evidence(ctx, "TESTWP-83")
    ctx.github.branch_heads[("test-aiflow-wp", "develop")] = "a" * 40
    project = ctx.repo().projects["TESTWP"]
    version = ctx.jira.version_data["TESTWP"][0]
    version_release.record_gate(ctx, project, version, version_release.evaluate(ctx, project, version))
    create_issue = ctx.jira.create_issue

    def fail_task(project_key, summary, *args, **kwargs):
        if summary.startswith("Merge "):
            raise RuntimeError("Jira 503")
        return create_issue(project_key, summary, *args, **kwargs)

    ctx.jira.create_issue = fail_task
    with pytest.raises(RuntimeError, match="Jira 503"):
        version_release.complete(ctx, project, version,
                                 {"id": "run-back", "fence": 6, "account_id": "claude-01", "node": "cc"},
                                 {"head_sha": "a" * 40, "checks": [{"command": "composer test", "passed": True}]})
    assert ctx.jira.released_versions == ["101"]
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action LIKE '%%merge-back%%'")["n"] == 0

    ctx.jira.create_issue = create_issue
    version_release.scan(ctx)
    version_release.scan(ctx)

    assert ctx.github.merges == []
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='jira-merge-back-task'")["n"] == 1


def test_release_notes_escape_markdown_in_the_jira_version_name():
    snapshot = {"version": "Core [x](https://evil.test) *0.5*\n# Owned", "tickets": []}

    notes = version_release._changelog(snapshot, "a" * 40, [])

    assert notes.splitlines()[0].startswith("## Core \\[x\\]\\(https\\:\\/\\/evil\\.test\\) \\*0\\.5\\* \\# Owned - ")
    assert "\n# Owned" not in notes


def test_ai_flow_repository_never_becomes_a_version_release_candidate(client, ctx):
    ctx.repo().projects["AIFLOW"] = Project(key="AIFLOW", repo="jooservices/ai-flow",
                                              version_release={"enabled": True, "versions": ["v4.0.0"]})
    ctx.jira.version_data["AIFLOW"] = [{"id": "900", "name": "v4.0.0", "released": False}]
    headers = node_headers(client)

    candidates = client.get("/v1/version-releases/candidates?account=claude-01", headers=headers).json()
    forbidden = client.post("/v1/version-releases/claim",
                            json={"project": "AIFLOW", "version_id": "900", "account": "claude-01"},
                            headers=headers)

    assert candidates["versions"] == []
    assert forbidden.status_code == 403
    assert getattr(ctx.github, "merges", []) == []
    assert getattr(ctx.github, "tags", {}) == {}
    assert getattr(ctx.github, "releases", {}) == {}


def test_testwp_default_release_scope_names_both_trial_versions(ctx):
    configure(ctx)
    project = ctx.repo().projects["TESTWP"]
    project.version_release = {}
    ctx.jira.version_data["TESTWP"] = [{"id": "101", "name": CORE, "released": False},
                                       {"id": "102", "name": THEME, "released": False},
                                       {"id": "103", "name": "WordPress Core 0.6.0", "released": False}]

    eligible = version_release.project_versions(ctx, project)

    assert [item["name"] for item in eligible] == [CORE, THEME]
    assert version_release.release_tag(project, CORE) == "testwp-wordpress-core-0.5.0-beta"


def test_manifest_update_pyproject_bumps_project_version_only():
    content = ('[build-system]\nrequires = ["hatchling"]\n\n[project]\nname = "demo"\nversion = "0.4.0"\n'
               'dependencies = ["requests>=2"]\n\n[tool.other]\nversion = "9.9.9"\n')
    updated = version_release._update_manifest("pyproject.toml", content, CORE)
    assert '[project]\nname = "demo"\nversion = "0.5.0-beta"\n' in updated
    assert '[tool.other]\nversion = "9.9.9"' in updated
    assert updated.count("0.5.0-beta") == 1


def test_manifest_update_cargo_bumps_package_version_not_dependencies():
    content = ('[package]\nname = "demo"\nversion = "0.4.0"\nedition = "2021"\n\n'
               '[dependencies]\nserde = { version = "1.0" }\n\n[dependencies.tokio]\nversion = "1.38"\n')
    updated = version_release._update_manifest("crates/demo/Cargo.toml", content, CORE)
    assert '[package]\nname = "demo"\nversion = "0.5.0-beta"\n' in updated
    assert 'serde = { version = "1.0" }' in updated
    assert '[dependencies.tokio]\nversion = "1.38"' in updated


def test_manifest_update_cargo_workspace_package():
    content = '[workspace]\nmembers = ["a"]\n\n[workspace.package]\nversion = "0.4.0"\n'
    updated = version_release._update_manifest("Cargo.toml", content, CORE)
    assert '[workspace.package]\nversion = "0.5.0-beta"\n' in updated


def test_manifest_update_chart_keeps_dependency_versions():
    content = ('apiVersion: v2\nname: demo\nversion: 0.4.0\nappVersion: "0.4.0"\n'
               'dependencies:\n  - name: redis\n    version: 17.3.0\n    repository: https://charts.example\n'
               '  - name: postgresql\n    version: "12.1.0"\n')
    updated = version_release._update_manifest("deploy/chart/Chart.yaml", content, CORE)
    assert 'version: "0.5.0-beta"\nappVersion: "0.5.0-beta"\n' in updated
    assert "    version: 17.3.0\n" in updated
    assert '    version: "12.1.0"\n' in updated
    assert updated.count("0.5.0-beta") == 2


def test_manifest_update_setup_cfg_and_csproj():
    cfg = "[metadata]\nname = demo\nversion = 0.4.0\n\n[options]\ninstall_requires =\n    x\n"
    assert 'version = "0.5.0-beta"' in version_release._update_manifest("setup.cfg", cfg, CORE)
    csproj = "<Project><PropertyGroup><Version>0.4.0</Version></PropertyGroup></Project>\n"
    assert "<Version>0.5.0-beta</Version>" in version_release._update_manifest("src/App.csproj", csproj, CORE)

