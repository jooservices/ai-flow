"""JAIF-418: a timed-out developer run with work goes to verification."""
from __future__ import annotations

from types import SimpleNamespace

from aiflow.v4.worker import dev
from aiflow.v4.worker.dev import Claimed


class _API:
    node_name = "cc"

    def __init__(self, existing_pr=None):
        self.calls = []
        self.existing_pr = existing_pr

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body if body is not None else params))
        if method == "GET" and path == "/v1/github/pr":
            if self.existing_pr:
                return {"found": True, **self.existing_pr}
            return {"found": False}
        if method == "POST" and path == "/v1/github/pr":
            return {"url": "https://github.com/jooservices/test-aiflow-wp/pull/9", "number": 9}
        return {"token": "github-token", "url": "https://github.com/x/y/pull/1", "number": 1}


def _claimed():
    project = {"key": "TESTWP", "repo": "jooservices/test-aiflow-wp", "base": "develop",
               "checks": ["uv run pytest tests/v4 -q"], "work_minutes": {"hard": 60},
               "merge": {"method": "squash"}}
    return Claimed(run_id="run-1", fence=3, run_token="t", account="claude-01", provider="claude",
                   model="opus", effort="", ticket={"key": "TESTWP-1", "project": "TESTWP",
                                                    "summary": "S", "description": "AC1", "labels": [],
                                                    "priority": "Medium", "type": "Bug"},
                   project=project, identity=("bot", "bot@example.test"))


def _run_timeout(monkeypatch, tmp_path, *, dirty=False, branch_commits=0, existing_pr=None):
    git_calls = []

    class Git:
        def __init__(self, *_a, **_k):
            pass

        def mirror(self, *_a):
            return "mirror"

        def foreign_worktree(self, *_a):
            return None

        def worktree(self, _m, _p, _b, base):
            return "origin/" + (_b if branch_commits else base)

        def commits_ahead(self, _path, _base):
            return branch_commits

        def merge_base_in(self, *_a):
            return []

        def push(self, _path, branch, token):
            git_calls.append(("push", branch, token))

        def run(self, *args, **kwargs):
            git_calls.append(("run", args))
            return "head-sha\n"

        def changed(self, _path):
            return ["aiflow/v4/core/work_limits.py"] if dirty else []

        def commit_all(self, _path, message, _identity):
            git_calls.append(("commit", message[:40]))
            return "commit-sha"

    class Adapter:
        argv = lambda self, _spec: []

        def run(self, *_a, **_k):
            return SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=0, quota={}, error_kind="timeout",
                                   reset_at=None, verdict="", verdict_reason="", error_message="hard limit",
                                   final_text="SUMMARY: still running tests\nTESTS: covering tests passed",
                                   permission_denials=[], exit_code=0, duration_s=3600, log_path="",
                                   command_results=[])

    monkeypatch.setattr(dev, "Git", Git)
    monkeypatch.setattr(dev, "get_adapter", lambda _p: Adapter())
    api = _API(existing_pr)
    api.git_calls = git_calls
    return dev.run_dev(api, _claimed(), str(tmp_path), "https://control", 3600, []), api


def _last_transition(api) -> str:
    return [call[2]["to"] for call in api.calls if str(call[1]).endswith("/transition")][-1]


def _comments(api) -> str:
    return "\n".join(str(call[2].get("body", "")) for call in api.calls if str(call[1]).endswith("/comment"))


def test_timeout_with_dirty_tree_commits_and_opens_a_draft_pr(monkeypatch, tmp_path):
    result, api = _run_timeout(monkeypatch, tmp_path, dirty=True)
    assert result["outcome"] == "timeout" and result["error_kind"] == "timeout"
    assert result["pr_url"].endswith("/pull/9")
    assert result["head_sha"] == "head-sha"
    assert result["details"]["full_suite"]["status"] == "unfinished"
    assert result["details"]["handed_to_pipeline"] is True
    assert ("commit",) == (api.git_calls[0][0],)
    assert ("push", "TESTWP-1-s", "github-token") in api.git_calls
    assert any(call[0] == "POST" and call[1] == "/v1/github/pr" for call in api.calls)
    assert _last_transition(api) == "In Verify"
    assert "no usable change" not in _comments(api)
    assert "full suite handed to pipeline" in _comments(api).lower() or "handed to pipeline" in _comments(api)


def test_timeout_with_existing_pr_reuses_it(monkeypatch, tmp_path):
    url = "https://github.com/jooservices/test-aiflow-wp/pull/42"
    result, api = _run_timeout(monkeypatch, tmp_path, existing_pr={"url": url, "number": 42, "head_sha": "pr-head"})
    assert result["outcome"] == "timeout" and result["error_kind"] == "timeout"
    assert result["pr_url"] == url
    assert result["head_sha"] == "pr-head"
    assert not any(call[0] == "POST" and call[1] == "/v1/github/pr" for call in api.calls)
    assert _last_transition(api) == "In Verify"
    assert "no usable change" not in _comments(api)


def test_timeout_with_branch_commits_opens_pr_and_verifies(monkeypatch, tmp_path):
    result, api = _run_timeout(monkeypatch, tmp_path, branch_commits=2)
    assert result["outcome"] == "timeout" and result["pr_url"].endswith("/pull/9")
    assert ("push", "TESTWP-1-s", "github-token") in api.git_calls
    assert any(call[0] == "POST" and call[1] == "/v1/github/pr" for call in api.calls)
    assert _last_transition(api) == "In Verify"


def test_timeout_with_empty_tree_returns_to_todo(monkeypatch, tmp_path):
    result, api = _run_timeout(monkeypatch, tmp_path)
    assert result["outcome"] == "timeout" and result["error_kind"] == "timeout"
    assert "pr_url" not in result
    assert _last_transition(api) == "To Do"
    assert "no usable change" in _comments(api)
    assert not any(call[0] == "POST" and call[1] == "/v1/github/pr" for call in api.calls)
