from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.control import version_release


VERSION = {"id": "101", "name": "Core 0.5.0"}
TICKET = {"key": "TESTWP-1", "summary": "A release ticket", "type": "Story"}
SNAPSHOT = {"ready": True, "version": VERSION["name"], "tickets": [TICKET], "reasons": []}
RUN = {"id": "run-release-check", "fence": 7, "account_id": "claude-01", "node": "node-1"}
REPORT = {"head_sha": "a" * 40, "source_sha": "a" * 40,
          "checks": [{"command": "check release", "passed": True}]}


class RecordingDb:
    def __init__(self, scenario, calls):
        self.scenario = scenario
        self.calls = calls

    def one(self, query, _params=()):
        self.calls.append("db.read:prior-check-failure")
        return self.scenario.get("prior_failure")

    def run(self, query, _params=()):
        if "UPDATE version_releases" in query:
            self.calls.append("db.write:failed-release-state")


class RecordingEvents:
    def __init__(self, calls):
        self.calls = calls

    def emit(self, name, **fields):
        self.calls.append(f"event:{name}:{json.dumps(fields, sort_keys=True, separators=(',', ':'))}")


class RecordingJira:
    def __init__(self, calls):
        self.calls = calls
        self.created = 0

    def create_issue(self, project, summary, _description, *, issuetype, labels):
        self.created += 1
        key = f"{project}-{self.created}"
        self.calls.append(f"jira.create_issue:{json.dumps({'project': project, 'summary': summary,
                                                           'description': _description, 'issuetype': issuetype,
                                                           'labels': labels}, sort_keys=True, separators=(',', ':'))}")
        return key

    def transition_to(self, key, status):
        self.calls.append(f"jira.transition:{key}:{status}")

    def link_issues(self, key, tracker, relation):
        self.calls.append(f"jira.link:{key}:{tracker}:{relation}")

    def comment(self, tracker, text):
        self.calls.append(f"jira.comment:{tracker}:{text}")


class RecordingGithub:
    def __init__(self, scenario, calls):
        self.scenario = scenario
        self.calls = calls

    def branch_sha(self, _repo, branch):
        self.calls.append(f"github.branch_sha:{branch}")
        if branch == "develop":
            return self.scenario.get("current_source", self.scenario.get("source_sha", "a" * 40))
        if branch.startswith("release/"):
            return self.scenario.get("release_head", self.scenario.get("head_sha", "a" * 40))
        return "b" * 40

    def branch_exists(self, _repo, branch):
        self.calls.append(f"github.branch_exists:{branch}")
        return self.scenario.get("branch_exists", True)

    def create_branch(self, _repo, branch, source):
        self.calls.append(f"github.create_branch:{branch}:{source}")

    def file_content(self, _repo, _branch, path):
        self.calls.append(f"github.file_content:{path}")
        return self.scenario.get("file_contents", {}).get(path)

    def commit_file(self, _repo, _branch, path, _content, message):
        self.calls.append(f"github.commit_file:{path}:{message}")
        return "commit-file"

    def release_pr(self, _repo, branch, target):
        self.calls.append(f"github.release_pr:{branch}:{target}")
        return self.scenario.get("pr")

    def reopen_pr(self, _repo, number):
        self.calls.append(f"github.reopen_pr:{number}")
        return {"number": number, "state": "open", "html_url": "https://example.test/pr/1"}

    def create_pr(self, _repo, branch, target, title, _body, *, draft):
        self.calls.append(f"github.create_pr:{branch}:{target}:{title}:{draft}")
        return {"number": 1, "state": "open", "html_url": "https://example.test/pr/1"}

    def update_pr_body(self, _repo, number, _body):
        self.calls.append(f"github.update_pr_body:{number}")

    def enable_merge_commit(self, _repo):
        self.calls.append("github.enable_merge_commit")

    def merge_pr(self, _repo, number, title, *, method):
        self.calls.append(f"github.merge_pr:{number}:{title}:{method}")
        return self.scenario.get("merge", {"merged": True, "sha": "b" * 40})

    def tag_sha(self, _repo, tag):
        self.calls.append(f"github.tag_sha:{tag}")
        return self.scenario.get("existing_tag_sha")

    def create_tag(self, _repo, tag, sha):
        self.calls.append(f"github.create_tag:{tag}:{sha}")

    def release_by_tag(self, _repo, tag):
        self.calls.append(f"github.release_by_tag:{tag}")
        return self.scenario.get("existing_release")

    def create_release(self, _repo, tag, _name, _notes):
        self.calls.append(f"github.create_release:{tag}")
        return {"html_url": "https://example.test/releases/1"}


def _case(name, **scenario):
    return {"name": name, **scenario}


CASES = [
    _case("already released", saved={"status": "released", "tag": "core-v0.5.0", "master_sha": "b" * 40,
                                     "github_release_url": "https://example.test/releases/1"}),
    _case("already released without github", github=False,
          saved={"status": "released", "tag": "core-v0.5.0", "master_sha": "b" * 40}),
    _case("prior check failure", prior_failure={"payload": {"bugs": [{"key": "TESTWP-8", "command": "lint"}]}}),
    _case("prior check failure with empty bugs", prior_failure={"payload": {"bugs": []}}),
    _case("missing github", github=False),
    _case("merge audit without target sha", merge_audit={"result": "", "payload": {}}),
    _case("merge audit without checked sha", saved={},
          merge_audit={"result": "b" * 40, "payload": {}}),
    _case("merge audit without saved row", merge_audit={"result": "b" * 40, "payload": {}}),
    _case("resume audited merge", merge_audit={"result": "b" * 40,
          "payload": {"checked_sha": "a" * 40, "pr_number": 3, "pr_url": "https://example.test/pr/3"}}),
    _case("live gate blocked", snapshot={"ready": False, "version": VERSION["name"], "tickets": [TICKET],
          "reasons": ["TESTWP-1: missing Done"]}),
    _case("failed check", report={"head_sha": "a" * 40, "checks": [
          {"command": "check release", "passed": False, "exit_code": 1, "output": "failed"}]}),
    _case("report preparation error", report={"head_sha": "a" * 40, "checks": [], "error": "checkout failed"}),
    _case("head below minimum length", report={"head_sha": "a" * 6, "checks": []}),
    _case("head at minimum length", current_source="a" * 7, release_head="a" * 7,
          report={"head_sha": "a" * 7, "checks": []}),
    _case("head above maximum length", report={"head_sha": "a" * 41, "checks": []}),
    _case("source below minimum length", report={"head_sha": "a" * 40, "source_sha": "a" * 6, "checks": []}),
    _case("source at minimum length", current_source="a" * 7, release_head="b" * 7,
          report={"head_sha": "b" * 7, "source_sha": "a" * 7, "checks": []}),
    _case("minimum short sha compared literally", current_source="a" * 40,
          report={"head_sha": "a" * 7, "source_sha": "a" * 7, "checks": REPORT["checks"]}),
    _case("source moved", current_source="b" * 40),
    _case("release branch moved", release_head="b" * 40),
    _case("create missing release branch", branch_exists=False),
    _case("unchanged release files", file_contents={"CHANGELOG.md": "new changelog", "README.md": "new readme"}),
    _case("commit changed release files", file_contents={"CHANGELOG.md": "old notes", "README.md": "old readme"}),
    _case("already merged pull request", pr={"number": 3, "state": "closed", "merged": True,
          "merge_commit_sha": "b" * 40, "html_url": "https://example.test/pr/3"}),
    _case("merged pull request without sha", pr={"number": 3, "state": "closed", "merged": True}),
    _case("reopen closed pull request", pr={"number": 3, "state": "closed", "merged": False,
          "html_url": "https://example.test/pr/3"}),
    _case("create pull request"),
    _case("merge refused", merge={"merged": False, "sha": "", "message": "branch protection"}),
    _case("conflicting existing tag", existing_tag_sha="c" * 40),
    _case("matching tag and existing release", existing_tag_sha="b" * 40,
          existing_release={"html_url": "https://example.test/releases/existing"}),
    _case("source comparison ignores case", source_sha="A" * 40, current_source="a" * 40,
          report={"head_sha": "A" * 40, "source_sha": "A" * 40, "checks": REPORT["checks"]}),
    _case("released row missing completion fields", saved={"status": "released", "tag": "", "master_sha": ""}),
]


def _run_case(monkeypatch, scenario):
    calls = []
    observed = {**scenario}
    if not observed.get("github", True):
        github = None
    else:
        github = RecordingGithub(observed, calls)
    project = SimpleNamespace(key="TESTWP", repo="jooservices/test-aiflow-wp", repo_name="test-aiflow-wp",
                              base="develop", version_release={}, tag_prefix=None)
    ctx = SimpleNamespace(db=RecordingDb(observed, calls), github=github, jira=RecordingJira(calls),
                          events=RecordingEvents(calls))
    saved = observed.get("saved")
    snapshot = observed.get("snapshot", SNAPSHOT)
    merge_audit = observed.get("merge_audit")

    monkeypatch.setattr(version_release, "tracker_for", lambda *_args: "TESTWP-REL")
    monkeypatch.setattr(version_release, "_tracker_row", lambda *_args: saved)
    monkeypatch.setattr(version_release, "_release_audit", lambda *_args: merge_audit)
    monkeypatch.setattr(version_release, "evaluate", lambda *_args: snapshot)
    monkeypatch.setattr(version_release, "_set_state",
                        lambda _ctx, _project, _version_id, **values: calls.append(
                            f"state:{ {key: getattr(value, 'obj', value) for key, value in values.items()} }"))
    monkeypatch.setattr(version_release, "_audit",
                        lambda _ctx, _project, _tracker, _run, action, result, _payload:
                        calls.append(f"audit:{json.dumps({'action': action, 'result': result, 'payload': _payload},
                                                         sort_keys=True, separators=(',', ':'))}"))
    monkeypatch.setattr(version_release, "_resumed_release_data",
                        lambda *_args: (snapshot, "resumed notes"))
    monkeypatch.setattr(version_release, "_prepare_release_files",
                        lambda _ctx, _project, _repo, branch, _snapshot, sha, checks:
                        (calls.append(f"prepare:{branch}:{sha}:{len(checks)}") or "release notes",
                         {"CHANGELOG.md": "new changelog", "README.md": "new readme"}))
    monkeypatch.setattr(version_release, "mark_jira_version_released_when_all_repositories_finish",
                        lambda *_args, **_kwargs: calls.append("jira.release_version_check") or True)
    monkeypatch.setattr(version_release, "merge_back",
                        lambda _ctx, _project, tracker, _name, tag, _run: calls.append(f"merge_back:{tracker}:{tag}"))
    return ctx, project, observed.get("report", REPORT), calls


def _observe(monkeypatch, case):
    ctx, project, report, calls = _run_case(monkeypatch, case)
    try:
        result = version_release.complete(ctx, project, VERSION, RUN, report)
    except Exception as exc:  # the exact refusal text is part of the contract
        result = {"exception": str(exc)}
    return {"result": result, "calls": calls}


FIXTURE = Path(__file__).parent / "fixtures" / "version_release_complete_parity.jsonl"
RECORDED_CASES = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
EXPECTED = {item["name"]: {"result": item["result"], "calls": item["calls"]} for item in RECORDED_CASES}


def test_fixture_covers_the_case_table():
    assert set(EXPECTED) == {case["name"] for case in CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_complete_matches_recorded_current_behavior(monkeypatch, case):
    observed = _observe(monkeypatch, case)
    assert observed == EXPECTED[case["name"]]
