"""Pipeline job coverage, worker prompts, and check-gap classification."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aiflow.v4.core import local_gate, pipeline_checks
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.worker import run_steps, verify
from aiflow.v4.control.ui import build_console_page


ROOT = Path(__file__).resolve().parents[2]
CI = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_every_workflow_job_is_a_project_check_or_has_a_pipeline_only_reason():
    assert pipeline_checks.unclassified_jobs(CI) == []
    assert pipeline_checks.catalog_errors() == []
    steps = {step.name: step for step in local_gate.steps("develop")}
    missing_steps = {name for job in pipeline_checks.PIPELINE_JOBS for name in job.check_steps} - set(steps)
    assert missing_steps == set()
    wrong_jobs = [(job.key, name) for job in pipeline_checks.PIPELINE_JOBS for name in job.check_steps
                  if steps[name].ci_job != job.key]
    assert wrong_jobs == []


def test_pipeline_only_jobs_have_reasons_and_are_visible_for_the_builtin_gate():
    checks, pipeline_only = pipeline_checks.project_catalog("ai-flow")
    assert any(row["job"] == "docs" for row in checks)
    assert {row["job"] for row in pipeline_only} >= {"security", "v4-browser", "acceptance-fast"}
    assert all(row["name"] and row["reason"].strip() for row in pipeline_only)
    assert pipeline_checks.project_catalog(["make test"]) == ([], [])


def test_pipeline_parity_reports_a_new_unclassified_workflow_job():
    workflow = CI + "\n  unclassified-example:\n    name: Unclassified\n    runs-on: ubuntu-latest\n    steps: []\n"
    assert pipeline_checks.unclassified_jobs(workflow) == ["unclassified-example"]


def test_failed_pipeline_only_job_is_reported_as_a_gap_in_project_checks():
    gaps = pipeline_checks.failure_gaps(["Security scan", "Giant files", "Unknown check"], "ai-flow")
    assert [row["job"] for row in gaps] == ["Security scan", "Unknown check"]
    assert "Docker" in gaps[0]["reason"]
    assert pipeline_checks.failure_gaps(["Giant files"], "")
    configured = pipeline_checks.failure_gaps(["Test (v4) shard 2"], "", ["pytest tests/v4 -q"])
    assert configured == []


def test_developer_and_verification_prompts_require_every_configured_check():
    checks = ["pytest -q", "ruff check src"]
    ticket = {"key": "PRJ-1", "project": "PRJ", "summary": "Check prompt", "description": ""}
    project = {"key": "PRJ", "repo": "org/repo", "base": "develop", "checks": checks, "environment": []}
    claim = SimpleNamespace(project=project, ticket=ticket, findings="", triage_answer="", triage_question="",
                            related=[], machine_tools={}, context_pack={})
    developer_prompt = run_steps.build_prompt(claim, "PRJ-1-work", env={})
    verify_prompt = verify.build_prompt(claim, {"number": 1, "branch": "PRJ-1-work"}, env={})
    for prompt in (developer_prompt, verify_prompt):
        assert all(command in prompt for command in checks)
        assert "even when it does not appear related to the changed paths" in prompt


def test_verify_executes_the_configured_local_gate_before_ci(monkeypatch, tmp_path):
    calls = []
    claim = SimpleNamespace(project={"key": "PRJ", "local_gate": "ai-flow"},
                            ticket={"key": "PRJ-2", "summary": "Check verify"})
    state = SimpleNamespace(claim=claim, run_as=["run-as"], result={"details": {}})
    workspace = SimpleNamespace(worktree=tmp_path)
    result = local_gate.GateResult([local_gate.StepResult("unit", local_gate.PASSED)])
    monkeypatch.setattr(verify, "project_environment", lambda *_args: {})
    monkeypatch.setattr(verify.gate_step, "run", lambda *args: calls.append(args) or result)

    assert verify.run_verify_gate(state, workspace) is None

    assert calls == [(tmp_path, claim.project, "feat: Check verify (PRJ-2)", {}, ["run-as"])]
    assert state.result["details"]["local_gate"] == [{"name": "unit", "status": "passed", "tail": ""}]


def test_ci_failure_report_includes_an_unrun_job_as_a_check_gap(monkeypatch):
    state = SimpleNamespace(api=object(), claim=SimpleNamespace(project={"local_gate": "ai-flow"}, run_id="run"),
                            pr={"head_sha": "sha"}, heartbeat=None, result={"details": {}})
    review = SimpleNamespace(checks="unit passed")
    ci = [{"name": "Security scan", "state": "failure", "url": "https://github.test/job"}]
    monkeypatch.setattr(verify, "wait_for_ci", lambda *_args: ci)
    monkeypatch.setattr(verify, "rework_verify", lambda _state, findings, why, _checks: {
        "findings": findings, "why": why})

    result = verify.collect_ci(state, SimpleNamespace(head="sha"), review)

    assert result["why"] == "CI failed"
    assert "CI Security scan: failed" in result["findings"]
    assert "Check gap: AI did not run Security scan" in result["findings"]
    assert state.result["details"]["check_gaps"][0]["job"] == "Security scan"

    ci[:] = [{"name": "Test (v4) shard 1", "state": "failure", "url": "https://github.test/job"}]
    covered = verify.collect_ci(state, SimpleNamespace(head="sha"), review)
    assert "Check gap" not in covered["findings"]
    assert not covered["findings"].endswith("\n")


def test_projects_screen_api_includes_commands_and_pipeline_only_reasons(client, ctx, control_repo):
    project_file = control_repo / "projects" / "TESTWP.yaml"
    project_file.write_text(project_file.read_text(encoding="utf-8") + "\nlocal_gate: ai-flow\n", encoding="utf-8")
    ctx.repo = lambda: ControlRepo.load(control_repo)

    response = client.get("/v1/projects")

    assert response.status_code == 200
    project = next(row for row in response.json()["projects"] if row["key"] == "TESTWP")
    assert project["check_commands"] == ["composer install", "vendor/bin/phpunit"]
    assert any(item["job"] == "docs" for item in project["pipeline_checks"])
    only = next(item for item in project["pipeline_only"] if item["job"] == "security")
    assert "Docker" in only["reason"]
    page = build_console_page()
    assert "pipeline only: " in page and "p.check_commands, p.pipeline_checks, p.pipeline_only" in page
