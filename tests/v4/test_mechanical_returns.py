"""Keep stale branches and named documentation-check failures out of verification."""
from __future__ import annotations

import subprocess
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.adapters import RunResult, RunSpec
from aiflow.v4.core.local_gate import GateResult, StepResult
from aiflow.v4.worker import dev, gate_step, mechanical, verify
from aiflow.v4.worker.gitops import Git, GitError


IDENTITY = ("AI Flow", "aiflow@example.test")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(cwd),
                                 "GIT_AUTHOR_NAME": IDENTITY[0], "GIT_AUTHOR_EMAIL": IDENTITY[1],
                                 "GIT_COMMITTER_NAME": IDENTITY[0], "GIT_COMMITTER_EMAIL": IDENTITY[1]})
    return result.stdout


def _real_ticket_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").strip()
    worktree = tmp_path / "ticket-worktree"
    git(repo, "worktree", "add", "-q", "-b", "ticket", str(worktree), "develop")
    (worktree / "implementation.py").write_text("original implementation\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "ticket implementation")
    return repo, worktree, base, git(worktree, "rev-parse", "HEAD").strip()


def _actual_git_run(tmp_path, worktree, implementation):
    claim = SimpleNamespace(
        ticket={"key": "JAIF-430", "summary": "Repair gate failure", "type": "Story"},
        identity=IDENTITY, run_id="run-real-gate", fence=3, account="dev", provider="codex", model="", step="dev",
    )
    return SimpleNamespace(
        api=SimpleNamespace(node_name="node"), claim=claim, srv_root=str(tmp_path / "srv"), key="JAIF-430",
        project={"checks": ["pytest"]}, worktree=worktree, env={}, run_as=[], git=Git(str(tmp_path / "srv")),
        heartbeat=SimpleNamespace(progress={}, lost=threading.Event(), checkpoint=lambda: [{"id": "guidance"}]),
        result={"details": {}}, response=RunResult(exit_code=0, final_text="RESULT: DONE", verdict="DONE"),
        summary="Implementation complete", tests="passed", handed={}, started_at=0,
        failed_work_commits=[implementation], failed_work_repair_base="",
        reporter=SimpleNamespace(comment=lambda **_kwargs: None, transition=lambda **_kwargs: None),
    )


def test_git_resolves_identical_conflict_stages_and_keeps_real_conflicts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    (repo / "same.txt").write_text("same\n")
    (repo / "real.txt").write_text("ours\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    same_blob = git(repo, "hash-object", "-w", "same.txt").strip()
    real_blob = git(repo, "hash-object", "-w", "real.txt").strip()
    index = (f"100644 {same_blob} 1\tsame.txt\n100644 {same_blob} 2\tsame.txt\n"
             f"100644 {same_blob} 3\tsame.txt\n100644 {real_blob} 1\treal.txt\n"
             f"100644 {real_blob} 2\treal.txt\n100644 {same_blob} 3\treal.txt\n")
    subprocess.run(["git", "update-index", "--index-info"], cwd=repo, input=index, check=True, text=True,
                   capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": str(repo)})

    unresolved = Git(str(tmp_path / "srv")).resolve_trivial_conflicts(repo)

    assert unresolved == ["real.txt"]
    assert (repo / "same.txt").read_text() == "same\n"
    assert "same.txt" not in git(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
    assert "real.txt" in git(repo, "diff", "--name-only", "--diff-filter=U").splitlines()


def test_git_resolves_conflict_path_with_git_quoted_display_name(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    name = 'same "quoted"\nname.txt'
    path = repo / name
    path.write_text("same\n")
    blob = git(repo, "hash-object", "-w", name).strip()
    index = b"".join(f"100644 {blob} {stage}\t".encode() + name.encode() + b"\0"
                     for stage in (1, 2, 3))
    subprocess.run(["git", "update-index", "-z", "--index-info"], cwd=repo, input=index, check=True,
                   capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": str(repo)})

    unresolved = Git(str(tmp_path / "srv")).resolve_trivial_conflicts(repo)

    assert unresolved == []
    assert path.read_text() == "same\n"


def test_git_merge_base_resolving_commits_a_clean_base_refresh(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "develop")
    (origin / "base.txt").write_text("base\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "base")
    git(origin, "checkout", "-q", "-b", "ticket")
    (origin / "feature.txt").write_text("feature\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "feature")
    git(origin, "checkout", "-q", "develop")
    (origin / "base.txt").write_text("develop update\n")
    git(origin, "commit", "-q", "-am", "develop update")
    worker_git = Git(str(tmp_path / "srv"))
    mirror = tmp_path / "srv" / "mirrors" / "repo.git"
    mirror.parent.mkdir(parents=True)
    worker_git.run("clone", "--bare", "-q", str(origin), str(mirror))
    worker_git.run("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=mirror)
    worker_git.run("fetch", "-q", "origin", cwd=mirror)
    worktree = tmp_path / "srv" / "work" / "dev"
    worker_git.worktree(mirror, worktree, "ticket", "develop")

    conflicts = worker_git.merge_base_resolving(worktree, "develop", IDENTITY)

    assert conflicts == []
    assert worker_git.behind(worktree, "develop") == 0
    assert (worktree / "base.txt").read_text() == "develop update\n"
    assert (worktree / "feature.txt").read_text() == "feature\n"


def test_developer_refresh_returns_real_conflict_files_to_development(monkeypatch, tmp_path):
    calls = []

    class FakeGit:
        def mirror(self, *_args):
            calls.append("fetch")

        def merge_base_resolving(self, *_args, **_kwargs):
            return ["src/one.py", "docs/v4/topic.md"]

        def abort_merge(self, _path):
            calls.append("abort")

        def push(self, *_args, **_kwargs):
            calls.append("preserve-branch")

        def behind(self, *_args):
            return 0

    run = SimpleNamespace(
        claim=SimpleNamespace(run_id="run-1", fence=3, identity=IDENTITY, ticket={"key": "JAIF-336"},
                              account="dev", provider="codex", model="", step="dev"),
        project={"base": "develop"}, repo="org/repo", branch="JAIF-336-work", worktree=tmp_path,
        git=FakeGit(), api=SimpleNamespace(call=lambda *_args, **_kwargs: {"token": "test-token"}),
        heartbeat=SimpleNamespace(progress={}), result={}, key="JAIF-336", started_at=0,
        branch_lease="checkout-head",
        reporter=SimpleNamespace(comment=lambda **kwargs: calls.append(kwargs["body"]),
                                 transition=lambda **kwargs: calls.append(kwargs["to"])),
    )
    monkeypatch.setattr(mechanical, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mechanical, "api_node", lambda _api: "node")

    outcome = mechanical.refresh_base(run)

    assert outcome["error_kind"] == "conflict"
    assert outcome["details"]["conflict_files"] == ["src/one.py", "docs/v4/topic.md"]
    assert "src/one.py" in next(item for item in calls if isinstance(item, str) and "src/one.py" in item)
    assert calls[-1] == "To Do"
    assert "preserve-branch" in calls


def test_conflict_preservation_requests_branch_token_and_uses_checkout_lease(monkeypatch, tmp_path):
    token_requests = []
    pushes = []

    class FakeGit:
        def mirror(self, *_args):
            pass

        def merge_base_resolving(self, *_args, **_kwargs):
            return ["src/change.py"]

        def abort_merge(self, _path):
            pass

        def push(self, path, branch, token, *, expected_head=None):
            pushes.append((branch, token, expected_head))

    def token_call(_method, endpoint, body):
        token_requests.append((endpoint, body))
        return {"token": "branch-capability" if body.get("branch") else "read-only-capability"}

    run = SimpleNamespace(
        claim=SimpleNamespace(run_id="run-2", fence=4, identity=IDENTITY, step="dev", account="dev",
                              provider="codex", model="",
                              ticket={"key": "JAIF-336"}),
        project={"base": "develop"}, repo="org/repo", branch="JAIF-336-work", worktree=tmp_path,
        git=FakeGit(), api=SimpleNamespace(call=token_call), heartbeat=SimpleNamespace(progress={}),
        result={}, key="JAIF-336", started_at=0, branch_lease="checkout-sha",
        reporter=SimpleNamespace(comment=lambda **_kwargs: None, transition=lambda **_kwargs: None),
    )
    monkeypatch.setattr(mechanical, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mechanical, "api_node", lambda _api: "node")

    mechanical.refresh_base(run)

    assert [body.get("branch") for _endpoint, body in token_requests] == [None, "JAIF-336-work"]
    assert pushes == [("JAIF-336-work", "branch-capability", "checkout-sha")]


@pytest.mark.parametrize(("start", "expected_lease"), [
    ("origin/JAIF-336-work", "checkout-sha"), ("origin/develop", ""),
])
def test_prepare_records_checkout_head_for_ticket_branch_lease(monkeypatch, tmp_path, start, expected_lease):
    class FakeGit:
        def mirror(self, *_args):
            return "mirror"

        def foreign_worktree(self, *_args):
            return None

        def worktree(self, *_args):
            return start

        def run(self, *_args, **_kwargs):
            return "checkout-sha\n"

        def commits_ahead(self, *_args):
            return 1

        def merge_base_in(self, *_args):
            return []

    run = SimpleNamespace(
        claim=SimpleNamespace(run_id="run-4", fence=4, identity=IDENTITY),
        project={"key": "JAIF", "base": "develop"}, repo="org/repo", branch="JAIF-336-work",
        srv_root=str(tmp_path), git=FakeGit(), api=SimpleNamespace(call=lambda *_args: {"token": "read-only"}),
        heartbeat=SimpleNamespace(), env={}, conflicts=[],
    )
    monkeypatch.setattr(dev, "worktree_path", lambda *_args: tmp_path / "work")
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(dev, "project_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(dev, "report_started", lambda *_args: None)
    monkeypatch.setattr(dev, "fetch_ci_log", lambda *_args: "")

    dev.prepare(run)

    assert run.checkout_head == "checkout-sha"
    assert run.branch_lease == expected_lease


def test_named_documentation_gap_is_the_only_gate_failure_retried():
    missing_page = GateResult([StepResult("docs-check", "failed", tail=(
        "documentation check failed: a console screen changed without a change in docs/v4/dashboard.md"))])
    other_failure = GateResult([StepResult("unit", "failed", tail="assertion failed")])
    unnamed_docs_failure = GateResult([StepResult("docs-check", "failed", tail="documentation site build failed")])

    assert mechanical.documentation_gap(missing_page)
    assert not mechanical.documentation_gap(other_failure)
    assert not mechanical.documentation_gap(unnamed_docs_failure)


def test_developer_repairs_named_docs_failure_then_reruns_gate(monkeypatch, tmp_path):
    gate_calls = []
    committed = []
    run = SimpleNamespace(
        claim=SimpleNamespace(ticket={"key": "JAIF-336", "summary": "Return mechanics", "type": "Story"},
                              identity=IDENTITY, run_id="run-1", account="dev", provider="codex", model=""),
        project={"checks": ["pytest"]}, worktree=tmp_path, env={}, run_as=[],
        heartbeat=SimpleNamespace(progress={}), result={"details": {}}, response=RunResult(verdict="DONE", exit_code=0),
        summary="Implementation complete", tests="passed", handed={},
        srv_root=str(tmp_path), key="JAIF-336",
        git=SimpleNamespace(changed=lambda _path: ["docs/v4/topic.md"],
                            run=lambda *_args, **_kwargs: "before-head",
                            commit_all=lambda *_args: committed.append("docs commit") or "docs-head"),
        failed_work_commits=[],
    )
    first = GateResult([StepResult("docs-check", "failed", tail=(
        "documentation check failed: a console screen changed without a change in docs/v4/topic.md"))])
    green = GateResult([StepResult("docs-check", "passed")])
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: gate_calls.append("gate") or
                        (first if len(gate_calls) == 1 else green))
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="ticket instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda _run, prepared: setattr(
        _run, "repair_prompt", prepared.spec.prompt))
    monkeypatch.setattr(dev.gate_step, "result_record", lambda result: {"status": "green"})

    result = dev.run_local_gate(run)

    assert result == ""
    assert gate_calls == ["gate", "gate"]
    assert committed == ["docs commit"]
    assert "docs-check" in run.repair_prompt and "docs/v4/topic.md" in run.repair_prompt
    assert run.result["details"]["local_gate"] == {"status": "green"}


def _documentation_repair_run(tmp_path, changed, repair_response, head_values=("head", "head")):
    heads = iter(head_values)
    committed = []
    run = SimpleNamespace(
        claim=SimpleNamespace(ticket={"key": "JAIF-336", "summary": "Return mechanics", "type": "Story"},
                              identity=IDENTITY, run_id="run-3", account="dev", provider="codex", model=""),
        project={"checks": ["pytest"]}, worktree=tmp_path, env={}, run_as=[],
        heartbeat=SimpleNamespace(progress={}, lost=threading.Event()),
        result={"tokens_in": 10, "tokens_out": 4, "cost_usd": 0.5,
                                                         "quota": {"used": 1}, "details": {}},
        response=RunResult(tokens_in=10, tokens_out=4, cost_usd=0.5, permission_denials=1,
                           quota={"used": 1}, token_usage_reported=True),
        summary="Implementation complete", tests="passed", handed={}, srv_root=str(tmp_path), key="JAIF-336",
        failed_work_commits=[],
        git=SimpleNamespace(changed=lambda _path: changed,
                            run=lambda *_args, **_kwargs: next(heads),
                            commit_all=lambda *_args: committed.append("docs commit") or "docs-head"),
    )
    return run, committed, repair_response


@pytest.mark.parametrize(("verdict", "error_kind", "accepted"), [("DONE", "", True), ("", "quota", False)])
def test_documentation_repair_aggregates_usage_and_latest_quota(monkeypatch, tmp_path, verdict, error_kind, accepted):
    from datetime import datetime, timezone

    observed = datetime(2026, 11, 9, tzinfo=timezone.utc)
    repair = RunResult(final_text="RESULT: DONE" if verdict else "", verdict=verdict, exit_code=0 if verdict else None,
                       error_kind=error_kind,
                       tokens_in=7, tokens_out=3, cost_usd=0.75,
                       permission_denials=2, quota={"used": 2}, quota_observed_at=observed,
                       token_usage_reported=True)
    run, committed, repair_response = _documentation_repair_run(
        tmp_path, ["docs/v4/topic.md", "deploy/v4/README.md"], repair)
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: setattr(target, "response", repair_response))

    result = dev.repair_documentation_gap(run, "missing docs")

    assert result is accepted
    assert run.result["tokens_in"] == 17 and run.result["tokens_out"] == 7
    assert run.result["cost_usd"] == 1.25
    assert run.result["quota"] == {"used": 2}
    assert run.result["quota_observed_at"] == observed.isoformat()
    assert run.result["details"]["permission_denials"] == 3
    assert run.response.tokens_in == 17 and run.response.cost_usd == 1.25
    assert bool(committed) is accepted


def test_documentation_repair_rejects_head_changes_even_when_only_docs_are_dirty(monkeypatch, tmp_path):
    repair = RunResult(verdict="DONE", exit_code=0, tokens_in=1)
    run, committed, repair_response = _documentation_repair_run(tmp_path, ["docs/v4/topic.md"], repair,
                                                                ("before", "after"))
    changed_calls = []
    run.git.changed = lambda _path: changed_calls.append(True) or ["docs/v4/topic.md"]
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: setattr(target, "response", repair_response))

    assert not dev.repair_documentation_gap(run, "missing docs")
    assert changed_calls == [] and committed == []


def test_documentation_repair_rejects_deployment_changes(monkeypatch, tmp_path):
    repair = RunResult(verdict="DONE", exit_code=0)
    run, committed, repair_response = _documentation_repair_run(
        tmp_path, ["docs/v4/topic.md", "deploy/v4/worker.yml"], repair)
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: setattr(target, "response", repair_response))

    assert not dev.repair_documentation_gap(run, "missing docs")
    assert committed == []


def _gate_repair_run(tmp_path, changed, repair_response, head_values=("head", "head")):
    heads = iter(head_values)
    committed = []
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run = SimpleNamespace(
        claim=SimpleNamespace(ticket={"key": "JAIF-430", "summary": "Repair gate failure", "type": "Story"},
                              identity=IDENTITY, run_id="run-gate", account="dev", provider="codex", model="",
                              step="dev", fence=3),
        project={"checks": ["pytest"]}, worktree=worktree, env={"PASSWORD": "gate-secret"}, run_as=[],
        heartbeat=SimpleNamespace(progress={}, lost=threading.Event()),
        result={"tokens_in": 10, "tokens_out": 4, "cost_usd": 0.5,
                                                         "quota": {"used": 1}, "details": {}},
        response=RunResult(tokens_in=10, tokens_out=4, cost_usd=0.5, permission_denials=1,
                           quota={"used": 1}, token_usage_reported=True, duration_s=2),
        summary="Implementation complete", tests="passed", handed={}, srv_root=str(tmp_path), key="JAIF-430",
        failed_work_commits=[],
        git=SimpleNamespace(changed=lambda _path: changed,
                            run=lambda *_args, **_kwargs: next(heads),
                            commit_all=lambda *_args: committed.append("repair commit") or "repair-head"),
    )
    return run, committed, repair_response


def test_failed_gate_repair_uses_same_worktree_and_reruns_once(monkeypatch, tmp_path):
    gate_calls, prompts = [], []
    repair = RunResult(final_text="RESULT: DONE", verdict="DONE", exit_code=0, tokens_in=7, tokens_out=3, cost_usd=0.75,
                       permission_denials=2, token_usage_reported=True, duration_s=3)
    run, committed, _ = _gate_repair_run(tmp_path, ["src/fix.py"], repair)
    first = GateResult([StepResult("unit", "failed", tail="assertion failed; expected gate-secret")])
    green = GateResult([StepResult("unit", "passed")])
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: gate_calls.append("gate") or
                        (first if len(gate_calls) == 1 else green))
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="ticket instructions", cwd=str(_run.worktree), account_home=str(tmp_path))))

    def run_ai(target, prepared):
        prompts.append((prepared.spec.cwd, prepared.spec.prompt))
        target.response = repair

    monkeypatch.setattr(dev, "run_ai_once", run_ai)

    outcome = dev.run_local_gate(run)

    assert outcome == ""
    assert len(prompts) == 1 and prompts[0][0] == str(run.worktree)
    assert "unit" in prompts[0][1] and "assertion failed" in prompts[0][1]
    assert "[redacted]" in prompts[0][1] and "gate-secret" not in prompts[0][1]
    assert gate_calls == ["gate", "gate"] and committed == ["repair commit"]
    assert run.result["tokens_in"] == 17 and run.result["tokens_out"] == 7
    assert run.result["cost_usd"] == 1.25 and run.response.permission_denials == 3
    assert run.result["details"]["duration_s"] == 5
    assert run.result["details"]["local_gate"]["status"] == "green"


def test_gate_repair_calls_fake_provider_once_and_saves_untracked_rejected_work(monkeypatch, tmp_path):
    repo, worktree, base, implementation = _real_ticket_worktree(tmp_path)
    run = _actual_git_run(tmp_path, worktree, implementation)
    calls, checkpoints, gates = [], [], []
    run.heartbeat.checkpoint = lambda: checkpoints.append(True) or [{"id": "new guidance"}]

    class FakeAdapter:
        def run(self, spec, _on_progress, stop):
            calls.append(stop)
            (Path(spec.cwd) / "repair-created.txt").write_text("untracked repair work\n")
            if len(calls) == 1:
                return RunResult(exit_code=0, final_text="", verdict="")
            return RunResult(exit_code=0, final_text="RESULT: DONE", verdict="DONE")

    adapter = FakeAdapter()
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    failed = GateResult([StepResult("unit", "failed", tail="assertion failed")])
    green = GateResult([StepResult("unit", "passed")])
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: (
        gates.append(True) or failed if len(gates) == 0 else gates.append(True) or green))
    monkeypatch.setattr(dev, "brief", lambda _run: dev.DevBrief(
        RunSpec(prompt="repair", cwd=str(worktree), account_home=str(tmp_path)), adapter,
        nullcontext(), False, None))

    result = dev.run_local_gate(run)

    assert result["outcome"] == "failed" and result["error_kind"] == "gate_failed"
    assert len(calls) == 1 and calls[0] is run.heartbeat.lost
    assert checkpoints == [] and gates == [True]
    saved = tmp_path / "srv" / "logs" / "runs" / "JAIF-430-run-real-gate.gate-failed.patch"
    clean = tmp_path / "clean-worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(clean), base)
    git(clean, "apply", str(saved))
    assert (clean / "implementation.py").read_text() == "original implementation\n"
    assert (clean / "repair-created.txt").read_text() == "untracked repair work\n"


@pytest.mark.parametrize(("lost_before", "lose_during", "calls_expected"), [
    (True, False, 0),
    (False, True, 1),
])
def test_single_repair_invocation_stops_when_lease_is_lost(lost_before, lose_during, calls_expected, tmp_path):
    lost = threading.Event()
    if lost_before:
        lost.set()
    calls = []

    class FakeAdapter:
        def run(self, _spec, _on_progress, stop):
            calls.append(stop)
            if lose_during:
                stop.set()
            return RunResult(exit_code=0, final_text="RESULT: DONE", verdict="DONE")

    run = SimpleNamespace(heartbeat=SimpleNamespace(lost=lost), api=SimpleNamespace())
    prepared = dev.DevBrief(RunSpec(prompt="repair", cwd=str(tmp_path), account_home=str(tmp_path)),
                            FakeAdapter(), nullcontext(), False, None)

    with pytest.raises(dev.LeaseLost):
        dev.run_ai_once(run, prepared)

    assert len(calls) == calls_expected


def test_repair_launch_failure_uses_gate_failure_preservation_path(monkeypatch, tmp_path):
    repo, worktree, base, implementation = _real_ticket_worktree(tmp_path)
    run = _actual_git_run(tmp_path, worktree, implementation)
    gate_calls = []

    class MissingProvider:
        def run(self, *_args, **_kwargs):
            raise FileNotFoundError("provider executable is missing")

    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: gate_calls.append(True) or
                        GateResult([StepResult("unit", "failed", tail="assertion failed")]))
    monkeypatch.setattr(dev, "brief", lambda _run: dev.DevBrief(
        RunSpec(prompt="repair", cwd=str(worktree), account_home=str(tmp_path)), MissingProvider(),
        nullcontext(), False, None))

    result = dev.run_local_gate(run)

    assert result["outcome"] == "failed" and result["error_kind"] == "gate_error"
    assert "could not start (FileNotFoundError)" in result["summary"]
    assert gate_calls == [True]
    assert run.failed_work_repair_base == implementation
    saved = tmp_path / "srv" / "logs" / "runs" / "JAIF-430-run-real-gate.gate-failed.patch"
    assert "original implementation" in saved.read_text()
    clean = tmp_path / "clean-worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(clean), base)
    git(clean, "apply", str(saved))
    assert (clean / "implementation.py").read_text() == "original implementation\n"


@pytest.mark.parametrize("failure", [dev.LeaseLost, dev.Conflict])
def test_repair_launch_preserves_lease_and_fence_exceptions(monkeypatch, tmp_path, failure):
    run, _committed, _repair = _gate_repair_run(tmp_path, ["src/fix.py"], RunResult())
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: GateResult([
        StepResult("unit", "failed", tail="assertion failed")]))
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev, "brief", lambda _run: (_ for _ in ()).throw(failure()))
    reports = []
    monkeypatch.setattr(dev, "report_gate_failure", lambda *_args: reports.append(True))

    with pytest.raises(failure):
        dev.run_local_gate(run)

    assert reports == []


def test_repair_launch_failure_after_lease_loss_still_stops_without_reporting(monkeypatch, tmp_path):
    run, _committed, _repair = _gate_repair_run(tmp_path, ["src/fix.py"], RunResult())
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: GateResult([
        StepResult("unit", "failed", tail="assertion failed")]))
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    reports = []
    monkeypatch.setattr(dev, "report_gate_failure", lambda *_args: reports.append(True))

    def fail_after_losing_lease(_run):
        run.heartbeat.lost.set()
        raise RuntimeError("provider startup failed")

    monkeypatch.setattr(dev, "brief", fail_after_losing_lease)

    with pytest.raises(dev.LeaseLost):
        dev.run_local_gate(run)

    assert reports == []


@pytest.mark.parametrize("repair_shape", ["multiple", "amended"])
def test_rejected_repair_saves_multiple_commits_and_amended_head(monkeypatch, tmp_path, repair_shape):
    repo, worktree, base, implementation = _real_ticket_worktree(tmp_path)
    run = _actual_git_run(tmp_path, worktree, implementation)

    class RepairAdapter:
        def run(self, _spec, _on_progress, stop):
            if repair_shape == "multiple":
                (worktree / "implementation.py").write_text("implementation after repair one\n")
                git(worktree, "add", "implementation.py")
                git(worktree, "commit", "-q", "-m", "repair one")
                (worktree / "repair-two.txt").write_text("repair two\n")
                git(worktree, "add", "repair-two.txt")
                git(worktree, "commit", "-q", "-m", "repair two")
            else:
                (worktree / "implementation.py").write_text("amended implementation\n")
                (worktree / "repair-amended.txt").write_text("amended repair\n")
                git(worktree, "add", "-A")
                git(worktree, "commit", "-q", "--amend", "--no-edit")
            return RunResult(exit_code=0, final_text="RESULT: DONE", verdict="DONE")

    adapter = RepairAdapter()
    monkeypatch.setattr(dev, "brief", lambda _run: dev.DevBrief(
        RunSpec(prompt="repair", cwd=str(worktree), account_home=str(tmp_path)), adapter,
        nullcontext(), False, None))

    assert not dev.repair_gate_failure(run, "unit", "assertion failed")
    assert run.failed_work_repair_base == implementation
    saved = Path(gate_step.keep_failed_work(
        run.git, worktree, tmp_path / f"{repair_shape}-failed", commits=run.failed_work_commits,
        repair_base=run.failed_work_repair_base))
    clean = tmp_path / f"clean-{repair_shape}"
    git(repo, "worktree", "add", "-q", "--detach", str(clean), base)
    git(clean, "apply", str(saved))
    if repair_shape == "multiple":
        assert (clean / "implementation.py").read_text() == "implementation after repair one\n"
        assert (clean / "repair-two.txt").read_text() == "repair two\n"
    else:
        assert (clean / "implementation.py").read_text() == "amended implementation\n"
        assert (clean / "repair-amended.txt").read_text() == "amended repair\n"


@pytest.mark.parametrize(
    ("repair", "changed", "heads", "accepted"),
    [
        (RunResult(final_text="RESULT: FAIL", verdict="FAIL"), ["src/fix.py"], ("head", "head"), False),
        (RunResult(final_text="RESULT: DONE", verdict="DONE"), [], ("head", "head"), False),
        (RunResult(final_text="RESULT: DONE", verdict="DONE"), ["src/fix.py"], ("head", "other-head"), False),
        (RunResult(final_text="RESULT: DONE", verdict="DONE", error_kind="provider"),
         ["src/fix.py"], ("head", "head"), False),
        (RunResult(final_text="RESULT: DONE", verdict="DONE", exit_code=1), ["src/fix.py"],
         ("head", "head"), False),
        (RunResult(final_text="RESULT: DONE", verdict="DONE", exit_code=0), ["src/fix.py"],
         ("head", "head"), True),
    ],
)
def test_gate_repair_accepts_only_done_with_unchanged_head_and_changes(
        monkeypatch, tmp_path, repair, changed, heads, accepted):
    run, committed, response = _gate_repair_run(tmp_path, changed, repair, heads)
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: setattr(target, "response", response))

    actual = dev.repair_gate_failure(run, "unit", "step unit failed:\nassertion failed")

    assert actual is accepted
    assert bool(committed) is accepted
    if heads[0] != heads[1]:
        assert run.failed_work_commits == []
        assert run.failed_work_repair_base == heads[0]
    elif accepted:
        assert run.failed_work_commits == ["repair-head"]


def test_gate_repair_records_repair_exit_code_over_successful_initial_exit(monkeypatch, tmp_path):
    repair = RunResult(final_text="RESULT: DONE", verdict="DONE", exit_code=17)
    run, _committed, _ = _gate_repair_run(tmp_path, ["src/fix.py"], repair)
    run.response.exit_code = 0
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: setattr(target, "response", repair))

    assert not dev.repair_gate_failure(run, "unit", "step unit failed: assertion failed")
    assert run.result["details"]["exit_code"] == 17


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        (GateResult([StepResult("slow", "timeout")]), "incomplete"),
        (GateResult([StepResult("db", "missing_env")]), "incomplete"),
    ],
)
def test_incomplete_only_gate_results_do_not_start_repair(monkeypatch, tmp_path, result, expected_status):
    calls = []
    run, _committed, _repair = _gate_repair_run(tmp_path, ["src/fix.py"], RunResult(verdict="DONE"))
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev, "run_ai_once", lambda *_args: calls.append("repair"))
    monkeypatch.setattr(dev, "report_gate_failure", lambda *_args: "refused")

    assert dev.run_local_gate(run) == "refused"
    assert calls == []
    assert run.result["details"]["local_gate"]["status"] == expected_status


def test_gate_error_does_not_start_repair(monkeypatch, tmp_path):
    calls = []
    run, _committed, _repair = _gate_repair_run(tmp_path, ["src/fix.py"], RunResult(verdict="DONE"))
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken")))
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev, "run_ai_once", lambda *_args: calls.append("repair"))
    monkeypatch.setattr(dev, "report_gate_failure", lambda *_args: "refused")

    assert dev.run_local_gate(run) == "refused"
    assert calls == []
    assert run.result["details"]["local_gate"]["status"] == "error"


def test_named_docs_gap_still_restricts_repair_to_documentation_paths(monkeypatch, tmp_path):
    run, committed, repair = _gate_repair_run(tmp_path, ["src/implementation.py"], RunResult(verdict="DONE"))
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: setattr(target, "response", repair))

    assert not dev.repair_documentation_gap(run, "step docs-check failed: missing docs/v4/topic.md")
    assert committed == []


def test_repeated_gate_failure_is_reported_after_one_repair_without_publication(monkeypatch, tmp_path):
    gate_calls, repairs, reports, pushes = [], [], [], []
    run, _committed, repair = _gate_repair_run(tmp_path, ["src/fix.py"],
                                                RunResult(final_text="RESULT: DONE", verdict="DONE", exit_code=0))
    first = GateResult([StepResult("unit", "failed", tail="first failure")])
    second = GateResult([StepResult("unit", "failed", tail="still failing")])
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    monkeypatch.setattr(dev.gate_step, "run", lambda *_args, **_kwargs: gate_calls.append("gate") or
                        (first if len(gate_calls) == 1 else second))
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: repairs.append("repair") or
                        setattr(target, "response", repair))
    monkeypatch.setattr(dev, "report_gate_failure", lambda _run, reason, kind: reports.append((reason, kind)) or
                        {"outcome": "failed"})
    run.git.commit_all = lambda *_args: "initial-commit"
    run.git.push = lambda *_args, **_kwargs: pushes.append("push")
    run.git.run = lambda *_args, **_kwargs: "head"
    run.changed = ["src/original.py"]
    run.repo, run.branch, run.project = "org/repo", "JAIF-430-work", {"base": "develop", "checks": ["pytest"]}
    run.api = SimpleNamespace(call=lambda *_args, **_kwargs: {"token": "unused"})
    monkeypatch.setattr(mechanical, "refresh_base", lambda _run: None)

    outcome = dev.publish_changes(run)

    assert outcome == {"outcome": "failed"}
    assert gate_calls == ["gate", "gate"] and repairs == ["repair"]
    assert reports and reports[0][1] == "gate_failed"
    assert pushes == []


def test_green_gate_repair_reaches_the_existing_push_and_pr_flow(monkeypatch, tmp_path):
    events, gates = [], []
    repair = RunResult(final_text="RESULT: DONE", verdict="DONE", exit_code=0, tokens_in=2)
    run, _committed, _ = _gate_repair_run(tmp_path, ["src/fix.py"], repair)
    first = GateResult([StepResult("unit", "failed", tail="assertion failed")])
    green = GateResult([StepResult("unit", "passed")])
    run.changed = ["src/original.py"]
    run.repo, run.branch, run.branch_lease = "org/repo", "JAIF-430-gate", "checkout-head"
    run.project = {"base": "develop", "checks": ["pytest"]}
    run.started_at, run.handed, run.tests = 0, {}, "passed"
    run.git.commit_all = lambda *_args: events.append("commit") or f"commit-{len(events)}"
    run.git.run = lambda *_args, **_kwargs: "published-head"
    run.git.push = lambda *_args, **_kwargs: events.append("push")
    run.api = SimpleNamespace(call=lambda _method, endpoint, _body: (
        events.append("token") or {"token": "test-token"}) if endpoint == "/v1/github/token" else
        events.append("pr") or {"url": "https://example.test/pr/430"})
    run.reporter = SimpleNamespace(comment=lambda **_kwargs: events.append("comment"),
                                   transition=lambda **kwargs: events.append(kwargs["to"]))
    monkeypatch.setattr(dev.gate_step, "plan", lambda _project: [object()])
    def run_gate(*_args, **_kwargs):
        gates.append("gate")
        events.append("gate")
        return first if len(gates) == 1 else green

    monkeypatch.setattr(dev.gate_step, "run", run_gate)
    monkeypatch.setattr(dev, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dev, "brief", lambda target: SimpleNamespace(
        spec=RunSpec(prompt="instructions", cwd=str(target.worktree), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: setattr(target, "response", repair))
    monkeypatch.setattr(mechanical, "refresh_base", lambda _run: None)

    outcome = dev.publish_changes(run)

    assert outcome["outcome"] == "done" and outcome["pr_url"] == "https://example.test/pr/430"
    assert gates == ["gate", "gate"]
    assert events.index("push") > events.index("gate")
    assert events.index("pr") > events.index("push")


def test_gate_output_redaction_removes_short_named_secrets_and_credentials():
    output = dev.redact_gate_output(
        "token=abc password=hunter2", {"TOKEN": "abc"}, {"environment": [{"name": "TOKEN", "kind": "secret"}]})

    assert output == "[server detail removed] [server detail removed]"


@pytest.mark.parametrize(("output", "value"), [
    ('{"api_key": "generated-api-value"}', "generated-api-value"),
    ("{'access_token': 'generated-access-value'}", "generated-access-value"),
    ('{"clientSecret": "generated-client-value"}', "generated-client-value"),
    ('{"private-key": "generated-private-value"}', "generated-private-value"),
    ("Authorization: Bearer generated-bearer-value", "generated-bearer-value"),
    ("Authorization: Basic generated-basic-value", "generated-basic-value"),
    ("database failed: postgresql://service:generated-url-password@db.test/app", "generated-url-password"),
    ('{"Authorization": "Basic generated-json-basic-value"}', "generated-json-basic-value"),
])
def test_gate_output_redaction_removes_quoted_and_header_credentials_not_in_environment(output, value):
    redacted = dev.redact_gate_output(output, {}, {})

    assert value not in redacted


def test_gate_repair_commit_error_rejects_repair(monkeypatch, tmp_path):
    run, committed, repair = _gate_repair_run(tmp_path, ["src/fix.py"],
                                                RunResult(final_text="RESULT: DONE", verdict="DONE", exit_code=0))
    run.git.commit_all = lambda *_args: (_ for _ in ()).throw(GitError("commit refused"))
    monkeypatch.setattr(dev, "brief", lambda _run: SimpleNamespace(
        spec=RunSpec(prompt="instructions", cwd=str(tmp_path), account_home=str(tmp_path))))
    monkeypatch.setattr(dev, "run_ai_once", lambda target, _prepared: setattr(target, "response", repair))

    assert not dev.repair_gate_failure(run, "unit", "step unit failed: issue")
    assert committed == []


def test_failed_gate_backup_saves_implementation_without_base_merge_delta(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "develop")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "checkout", "-q", "-b", "ticket")
    (repo / "implementation.py").write_text("implementation\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "implementation")
    implementation = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "develop")
    (repo / "upstream-only.txt").write_text("base merge delta\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "upstream update")
    git(repo, "checkout", "-q", "ticket")
    git(repo, "merge", "-q", "--no-edit", "develop")

    saved = Path(gate_step.keep_failed_work(Git(str(tmp_path / "srv")), repo, tmp_path / "logs" / "run",
                                            commits=[implementation]))
    patch_text = saved.read_text()

    assert "implementation.py" in patch_text and "+implementation" in patch_text
    assert "upstream-only.txt" not in patch_text and "base merge delta" not in patch_text


def test_existing_work_refreshes_and_passes_gate_before_branch_push(monkeypatch, tmp_path):
    calls = []
    run = SimpleNamespace(
        claim=SimpleNamespace(run_id="run-1", fence=3, ticket={"key": "JAIF-336", "summary": "Return mechanics"},
                              account="dev", provider="codex", model="", step="dev"),
        project={"base": "develop"}, repo="org/repo", branch="JAIF-336-work", branch_commit_count=1,
        worktree=tmp_path, summary="Done", tests="passed", handed={}, started_at=0,
        branch_lease="checkout-sha",
        git=SimpleNamespace(push=lambda *_args, **_kwargs: calls.append("push"),
                            run=lambda *_args, **_kwargs: "new-head"),
        api=SimpleNamespace(call=lambda *_args, **_kwargs: calls.append("token") or {"token": "test-token"}),
        result={"details": {}}, reporter=SimpleNamespace(comment=lambda **_kwargs: None,
                                                             transition=lambda **_kwargs: None),
    )
    monkeypatch.setattr(mechanical, "refresh_base", lambda _run: calls.append("refresh"))
    monkeypatch.setattr(dev, "run_local_gate", lambda _run: calls.append("gate") or "")

    result = dev.publish_existing_work(run, {"url": "https://example.test/pr/1"})

    assert result["outcome"] == "done"
    assert calls.index("refresh") < calls.index("gate") < calls.index("push")
    assert result["head_sha"] == "new-head"


def test_verification_refuses_to_start_when_branch_update_did_not_finish(monkeypatch, tmp_path):
    started = []

    class FakeGit:
        def mirror(self, *_args):
            return "mirror"

        def remote_branch_exists(self, *_args):
            return True

        def detached(self, *_args):
            return "stale-head"

        def merge_base_resolving(self, *_args, **_kwargs):
            return []

        def behind(self, *_args):
            return 2

        def abort_merge(self, _path):
            pass

    state = SimpleNamespace(
        claim=SimpleNamespace(run_id="run-1", fence=1, identity=IDENTITY,
                              project={"key": "JAIF", "repo": "org/repo", "base": "develop"},
                              ticket={"key": "JAIF-336"}),
        pr={"number": 10, "branch": "JAIF-336-work", "url": "https://example.test/pr/10"},
        api=SimpleNamespace(call=lambda *_args, **_kwargs: {"token": "test-token"}),
        git=FakeGit(), srv_root=str(tmp_path), heartbeat=SimpleNamespace(), result={}, started_at=0,
        reporter=SimpleNamespace(comment=lambda **_kwargs: started.append("comment")),
    )
    monkeypatch.setattr(verify, "set_run_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verify, "worktree_path", lambda *_args, **_kwargs: tmp_path / "verify")
    monkeypatch.setattr(verify, "bring_up_to_date", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(verify, "report_verify_started", lambda *_args: started.append("verify-started"))

    outcome = verify.prepare_verify(state)

    assert outcome["error_kind"] == "stale_branch"
    assert "verify-started" not in started
