"""Repository selection passed from project configuration into each worker checkout."""
from __future__ import annotations

from types import SimpleNamespace

from aiflow.v4.core.controlrepo import Project, epic_for_ticket
from aiflow.v4.control import version_release
from aiflow.v4.worker import dev, verify
from aiflow.v4.worker.dev import Claimed


def test_repo_lookup_uses_epic_mapping_and_project_defaults():
    project = Project(key="JOOWP", repo="jooservices/wordpress-default", base="develop",
                      stack="php-8.3", checks=["composer install"], repositories=[
                          {"repo": "jooservices/wordpress-core", "base": "main", "stack": "python-3.12",
                           "checks": ["pytest"]}],
                      epic_repositories={"JOOWP-2": "jooservices/wordpress-core"})
    hierarchy = {
        "JOOWP-3": {"key": "JOOWP-3", "type": "Story", "parent_key": "JOOWP-2"},
        "JOOWP-2": {"key": "JOOWP-2", "type": "Epic", "parent_key": None},
    }
    epic = epic_for_ticket({"key": "JOOWP-4", "type": "Sub-task", "parent_key": "JOOWP-3"}, hierarchy)
    assert epic == "JOOWP-2"
    selected = project.settings_for_ticket({"key": "JOOWP-4", "epic": epic})
    assert selected == {"repo": "jooservices/wordpress-core", "base": "main", "checks": ["pytest"],
                        "stack": "python-3.12", "containers": False}
    assert project.settings_for_ticket({"key": "JOOWP-5"}) == {
        "repo": "jooservices/wordpress-default", "base": "develop", "checks": ["composer install"],
        "stack": "php-8.3", "containers": False}


def test_version_release_project_uses_the_repository_selected_by_version_tickets():
    project = Project(key="JOOWP", repo="jooservices/wordpress-core", base="develop",
                      checks=["default checks"], version_release={"enabled": True, "versions": ["Core"]},
                      repositories=[{"repo": "jooservices/wordpress-mcp", "base": "main",
                                     "checks": ["npm test"], "stack": "node-24"}],
                      epic_repositories={"JOOWP-306": "jooservices/wordpress-mcp"})
    selected = version_release.project_for_version(None, project,
                                                   {"id": "306", "repository": "jooservices/wordpress-mcp",
                                                    "repositories": ["jooservices/wordpress-mcp"]})
    assert selected.repo == "jooservices/wordpress-mcp"
    assert selected.base == "main" and selected.checks == ["npm test"]


def test_jira_version_release_targets_the_repository_of_its_epic_tickets():
    class DB:
        def all(self, _query, _params):
            return [
                {"key": "JOOWP-2", "type": "Epic", "parent_key": None},
                {"key": "JOOWP-4", "type": "Story", "parent_key": "JOOWP-2"},
            ]

    class Jira:
        def version_issues(self, project, version_id):
            assert (project, version_id) == ("JOOWP", "101")
            return [{"key": "JOOWP-4", "fields": {"issuetype": {"name": "Story"},
                                                     "parent": {"key": "JOOWP-2"}}}]

    project = Project(key="JOOWP", repo="jooservices/wordpress-theme", base="develop",
                      repositories=[{"repo": "jooservices/wordpress-core", "base": "main"}],
                      epic_repositories={"JOOWP-2": "jooservices/wordpress-core"})
    ctx = SimpleNamespace(jira=Jira(), db=DB())
    assert version_release.repositories_for_version(ctx, project, {"id": "101"}) == [
        "jooservices/wordpress-core"]


def test_jira_version_membership_is_partitioned_by_repository_for_release_gates(monkeypatch):
    class DB:
        def all(self, _query, _params):
            return [
                {"key": "JOOWP-2", "type": "Epic", "parent_key": None},
                {"key": "JOOWP-3", "type": "Epic", "parent_key": None},
                {"key": "JOOWP-20", "type": "Story", "parent_key": "JOOWP-2"},
                {"key": "JOOWP-30", "type": "Story", "parent_key": "JOOWP-3"},
            ]

        def one(self, _query, _params):
            return None

    class Jira:
        def version_issues(self, _project, _version_id):
            return [
                {"key": "JOOWP-20", "fields": {"summary": "Core work", "status": {"name": "Done"},
                                                   "issuetype": {"name": "Story"},
                                                   "parent": {"key": "JOOWP-2"}}},
                {"key": "JOOWP-30", "fields": {"summary": "Theme work", "status": {"name": "Done"},
                                                   "issuetype": {"name": "Story"},
                                                   "parent": {"key": "JOOWP-3"}}},
            ]

    monkeypatch.setattr(version_release, "_successful_evidence", lambda *_args: (True, True, True))
    monkeypatch.setattr(version_release, "check_commands", lambda _project: ["pytest"])
    project = Project(key="JOOWP", repo="jooservices/wordpress-theme", checks=["pytest"],
                      repositories=[{"repo": "jooservices/wordpress-core", "checks": ["pytest"]}],
                      epic_repositories={"JOOWP-2": "jooservices/wordpress-core"})
    version = {"id": "101", "name": "1.0", "repository": "jooservices/wordpress-core",
               "repositories": ["jooservices/wordpress-core", "jooservices/wordpress-theme"]}
    target = version_release.project_for_version(None, project, version)
    snapshot = version_release.evaluate(SimpleNamespace(jira=Jira(), db=DB()), target, version)

    assert snapshot["ready"] is True
    assert snapshot["repository"] == "jooservices/wordpress-core"
    assert [ticket["key"] for ticket in snapshot["tickets"]] == ["JOOWP-20"]


def test_version_release_state_updates_are_scoped_to_repository():
    class DB:
        query = ""
        params = ()

        def run(self, query, params):
            self.query, self.params = query, params

    project = Project(key="JOOWP", repo="jooservices/wordpress-core")
    db = DB()
    version_release._set_state(SimpleNamespace(db=db), project, "101", status="released")

    assert "repository=%s" in db.query
    assert db.params == ("released", "JOOWP", "101", "jooservices/wordpress-core")


def test_shared_jira_version_is_released_only_after_each_repository_finishes(monkeypatch):
    project = Project(key="JOOWP", repo="jooservices/wordpress-theme", checks=["pytest"],
                      repositories=[{"repo": "jooservices/wordpress-core", "checks": ["pytest"]}])
    states = {"jooservices/wordpress-core": {"status": "released"},
              "jooservices/wordpress-theme": {"status": "pending"}}
    released = []

    class Jira:
        def versions(self, _project):
            return [{"id": "101", "name": "1.0", "released": False}]

        def release_version(self, version_id):
            released.append(version_id)

    monkeypatch.setattr(version_release, "_tracker_row",
                        lambda _ctx, target, _version_id: states.get(target.repo))
    ctx = SimpleNamespace(jira=Jira())
    version = {"id": "101", "repositories": ["jooservices/wordpress-core", "jooservices/wordpress-theme"]}

    assert not version_release.mark_jira_version_released_when_all_repositories_finish(ctx, project, version)
    assert released == []
    states["jooservices/wordpress-theme"] = {"status": "released"}
    assert version_release.mark_jira_version_released_when_all_repositories_finish(ctx, project, version)
    assert released == ["101"]


class _API:
    node_name = "node1"

    def __init__(self, response_by_path):
        self.response_by_path = response_by_path
        self.calls = []

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body, params))
        response = self.response_by_path.get(path)
        return response(body) if callable(response) else (response or {})


def _adapter(final_text):
    class Adapter:
        argv = lambda self, _spec: []

        def run(self, *_args, **_kwargs):
            return SimpleNamespace(tokens_in=1, tokens_out=2, cost_usd=0, quota={}, error_kind="", reset_at=None,
                                   verdict="PASS", verdict_reason="", error_message="", final_text=final_text,
                                   permission_denials=[], exit_code=0, duration_s=1, log_path="run.jsonl")
    return Adapter()


def _claim(project):
    return Claimed(run_id="run-1", fence=7, run_token="run-token", account="codex-01", provider="codex",
                   model="model", effort="high",
                   ticket={"key": "JOOWP-4", "project": "JOOWP", "summary": "Do work", "description": "AC1",
                           "labels": [], "priority": "Medium", "type": "Story", "parent_key": "JOOWP-3",
                           "epic": "JOOWP-2"},
                   project=project, identity=("bot", "bot@example.test"))


def test_dev_worker_checks_out_mapped_repository_and_base(monkeypatch, tmp_path):
    calls = {"mirrors": [], "worktrees": [], "pushes": [], "prs": []}

    class Git:
        def __init__(self, *_args, **_kwargs):
            pass

        def mirror(self, repo, token):
            calls["mirrors"].append((repo, token))
            return "mirror"

        def foreign_worktree(self, *_a):
            return None

        def worktree(self, mirror, path, branch, base):
            calls["worktrees"].append((mirror, str(path), branch, base))
            return "origin/" + base

        def changed(self, _path):
            return ["README.md"]

        def commit_all(self, *_args):
            return "a" * 40

        def push(self, _path, branch, token):
            calls["pushes"].append((branch, token))

    api = _API({"/v1/github/token": {"token": "github-token"},
                "/v1/github/pr": lambda body: (calls["prs"].append(body) or {"url": "https://github.com/jooservices/wordpress-core/pull/4", "number": 4})})
    monkeypatch.setattr(dev, "Git", Git)
    monkeypatch.setattr(dev, "get_adapter", lambda _provider: _adapter("SUMMARY: implemented\nTESTS: pytest passed"))
    project = {"key": "JOOWP", "repo": "jooservices/wordpress-core", "base": "main", "checks": ["pytest"],
               "work_minutes": {"hard": 60}, "merge": {"method": "squash"}}

    result = dev.run_dev(api, _claim(project), str(tmp_path), "https://control", 3600, [])

    assert result["outcome"] == "done"
    assert calls["mirrors"] == [("jooservices/wordpress-core", "github-token")]
    assert calls["worktrees"][0][3] == "main"
    assert calls["prs"][0]["repo"] == "jooservices/wordpress-core"
    assert calls["prs"][0]["base"] == "main"


def test_verify_worker_uses_dev_repository_for_checkout_and_merge_base(monkeypatch, tmp_path):
    calls = {"mirrors": [], "bases": [], "scrubs": []}

    class Git:
        def __init__(self, *_args, **_kwargs):
            pass

        def mirror(self, repo, token):
            calls["mirrors"].append((repo, token))
            return "mirror"

        def remote_branch_exists(self, _mirror, _branch):
            return True

        def detached(self, _mirror, _path, ref):
            return ref

        def merge_base_in(self, _path, base, _identity, commit=False):
            calls["bases"].append((base, commit))
            return []

        def scrub(self, path):
            calls["scrubs"].append(str(path))

    api = _API({"/v1/github/token": {"token": "github-token"}, "/v1/github/checks": {"checks": []},
                "/v1/github/merge": {"merged": True, "sha": "b" * 40}})
    monkeypatch.setattr(verify, "Git", Git)
    monkeypatch.setattr(verify, "get_adapter", lambda _provider: _adapter("VERDICT: PASS\nCHECKS: pytest passed"))
    project = {"key": "JOOWP", "repo": "jooservices/wordpress-core", "base": "main", "checks": ["pytest"],
               "work_minutes": {"hard": 60}, "merge": {"method": "squash", "auto": True,
                                                          "require_human": False}}
    pr = {"number": 4, "branch": "JOOWP-4-work", "url": "https://github.com/jooservices/wordpress-core/pull/4",
          "head_sha": "a" * 40}

    result = verify.run_verify(api, _claim(project), pr, str(tmp_path), "https://control", 3600, [])

    assert result["outcome"] == "merged"
    assert calls["mirrors"] == [("jooservices/wordpress-core", "github-token")]
    assert calls["bases"] == [("main", False)]
    assert calls["scrubs"]
    assert any(path == "/v1/github/merge" for _, path, _, _ in api.calls)
