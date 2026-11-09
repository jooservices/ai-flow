"""JAIF-591: a developer run executes the tests covering its changes; verification reuses green pipeline checks."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aiflow.v4.core import local_gate
from aiflow.v4.worker import dev, verify
from aiflow.v4.worker.gitops import GitError

ROOT = Path(__file__).resolve().parents[2]


def _step(plan, name):
    return next(step for step in plan if step.name == name)


def test_changed_files_select_their_covering_tests():
    tests = dev.covering_tests(["aiflow/v4/worker/intake.py"], ROOT)
    assert tests is not None and "tests/v4/test_intake.py" in tests
    assert len(tests) < len(list((ROOT / "tests/v4").glob("test_*.py")))


def test_shared_code_selects_the_full_suite():
    assert dev.covering_tests(["aiflow/v4/core/local_gate.py"], ROOT) is None
    assert dev.covering_tests([], ROOT) is None


def test_focused_plan_replaces_the_full_suite_and_drops_the_scale_proof():
    plan = dev.focused_gate_plan("develop", ("tests/v4/test_intake.py",))
    assert _step(plan, "v4").argv == ("uv", "run", "--frozen", "pytest", "tests/v4/test_intake.py", "-p", "no:warnings")
    assert "scale-proof" not in [step.name for step in plan]
    assert _step(plan, "quality").argv == _step(local_gate.steps("develop"), "quality").argv


def test_focused_plan_keeps_the_scale_proof_when_its_test_changed():
    tests = ("tests/v4/test_scale_proof.py", "tests/v4/test_intake.py")
    plan = dev.focused_gate_plan("develop", tests)
    assert "scale-proof" in [step.name for step in plan]
    assert "tests/v4/test_scale_proof.py" not in _step(plan, "v4").argv


class _Git:
    def __init__(self, output="", error=False):
        self.output, self.error, self.calls = output, error, []

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if self.error:
            raise GitError("no base")
        return self.output


def _run(project, git):
    return SimpleNamespace(project=project, git=git, worktree=ROOT, env={}, run_as=[],
                           claim=SimpleNamespace(ticket={"key": "JAIF-1", "summary": "Focus"}), result={"details": {}})


def test_dev_gate_runs_only_the_covering_tests(monkeypatch):
    ran = []
    monkeypatch.setattr(dev, "run_gate_plan", lambda *args: ran.append(args[-1]) or local_gate.GateResult())
    run = _run({"local_gate": "ai-flow", "base": "develop"}, _Git("aiflow/v4/worker/intake.py\n"))

    dev.run_gate_steps(run)

    assert run.git.calls == [("diff", "--name-only", "origin/develop...HEAD")]
    assert "tests/v4/test_intake.py" in _step(ran[0], "v4").argv
    assert "tests/v4/test_intake.py" in run.result["details"]["focused_tests"]


def test_dev_gate_falls_back_to_the_full_gate_when_the_changes_are_unknown_or_the_gate_is_custom(monkeypatch):
    full = []
    monkeypatch.setattr(dev.gate_step, "run", lambda *args: full.append(args) or local_gate.GateResult())
    dev.run_gate_steps(_run({"local_gate": "ai-flow"}, _Git(error=True)))
    git = _Git()
    dev.run_gate_steps(_run({"checks": ["pytest"]}, git))
    assert len(full) == 2 and git.calls == []


def _ci(**states):
    return [{"name": name, "state": state} for name, state in states.items()]


def test_green_pipeline_jobs_need_every_matching_check_to_pass():
    ci = _ci(**{"Test (v4) shard 1": "success", "Test (v4) shard 2": "failure", "Documentation": "success",
                "Code quality": "pending", "Giant files": "neutral"})
    assert verify.green_pipeline_jobs(ci) == ["docs", "giant-files"]
    assert verify.green_pipeline_jobs([]) == []


def test_green_jobs_are_not_repeated_by_verification():
    plan, reused = verify.split_reused_steps(local_gate.steps("develop"), ["docs", "v4-shard"])
    assert reused == ["docs-build", "docs-check", "v4"]
    assert "v4" not in [step.name for step in plan] and "headers" in [step.name for step in plan]


def test_verification_runs_the_remaining_steps_and_records_the_reused_ones(monkeypatch, tmp_path):
    ran = []
    claim = SimpleNamespace(project={"key": "PRJ", "local_gate": "ai-flow", "base": "develop"},
                            ticket={"key": "PRJ-2", "summary": "Reuse"})
    state = SimpleNamespace(claim=claim, run_as=[], result={"details": {}})
    monkeypatch.setattr(verify, "project_environment", lambda *_args: {})
    monkeypatch.setattr(verify, "run_gate_plan", lambda *args: ran.append(args[-1]) or local_gate.GateResult())

    assert verify.run_verify_gate(state, SimpleNamespace(worktree=tmp_path), ["v4-shard", "docs"]) is None

    assert "v4" not in [step.name for step in ran[0]]
    assert state.result["details"]["reused_pipeline_checks"] == ["docs-build", "docs-check", "v4"]


def test_ci_status_that_cannot_be_read_reuses_nothing():
    api = SimpleNamespace(call=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    claim = SimpleNamespace(run_id="r", fence=1)
    assert verify.fetch_ci_checks(api, claim, "sha") == []
