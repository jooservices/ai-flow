"""SonarCloud analysis on pull requests (JAIF-576)."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
CI = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))
CI_TEXT = CI_PATH.read_text(encoding="utf-8")
SONAR_PROPERTIES = ROOT / "sonar-project.properties"
README = ROOT / "README.md"
DOC = ROOT / "docs" / "v4" / "sonarcloud.md"
TOPIC = ROOT / "deploy" / "v4" / "topics" / "sonarcloud.md"


def test_sonar_project_properties_targets_ai_flow_python_sources():
    text = SONAR_PROPERTIES.read_text(encoding="utf-8")
    assert "sonar.projectKey=jooservices_ai-flow" in text
    assert "sonar.organization=jooservices" in text
    assert "sonar.sources=aiflow" in text
    assert "sonar.tests=tests" in text
    assert "coverage" not in text  # coverage reaches the quality gate later through the Codecov job (JAIF-574)
    assert "sonar.exclusions=" in text
    assert "docs/v4/events.md" in text


def test_ci_sonar_job_runs_in_parallel_uses_org_secrets_and_skips_bots():
    job = CI["jobs"]["sonar"]
    assert job["name"] == "SonarCloud"
    assert "needs" not in job  # not on the critical path of the tests
    condition = str(job.get("if", ""))
    assert "schedule" in condition
    assert all("github.event.pull_request.user.type != 'Bot'" in str(step.get("if", "")) for step in job["steps"][1:])
    step_names = [step.get("name") for step in job["steps"]]
    assert "Require SonarCloud token" in step_names
    assert "Resolve SonarQube host URL" in step_names
    assert "Upload SonarQube analysis" in step_names
    assert "Merge coverage reports" not in step_names
    require = next(step for step in job["steps"] if step.get("name") == "Require SonarCloud token")
    assert "SONAR_TOKEN is not configured" in require["run"]
    # Fail with a clear message; never echo or dump the secret value.
    assert "echo \"$SONAR_TOKEN\"" not in require["run"]
    assert "printenv" not in require["run"]
    assert "secrets.SONAR_TOKEN" in str(require.get("env", {}))
    assert "secrets.SONAR_TOKEN" not in require["run"]
    resolve = next(step for step in job["steps"] if step.get("name") == "Resolve SonarQube host URL")
    assert "secrets.SONAR_HOST_URL" in str(resolve.get("env", {}))
    assert 'host_url="${SONAR_HOST_URL:-https://sonarcloud.io}"' in resolve["run"]
    scan = next(step for step in job["steps"] if step.get("name") == "Upload SonarQube analysis")
    assert "SonarSource/sonarqube-scan-action@" in scan["uses"]
    assert "secrets.SONAR_TOKEN" in str(scan.get("env", {}))
    assert "steps.sonar-config.outputs.host_url" in str(scan.get("env", {}))


def test_v4_shards_do_not_run_under_coverage():
    shard = CI["jobs"]["v4-shard"]
    runs = "\n".join(str(step.get("run", "")) for step in shard["steps"])
    assert "coverage run" not in runs and "COVERAGE_FILE" not in runs
    assert not any("coverage" in str(step.get("name", "")).casefold() for step in shard["steps"])


def test_readme_has_sonarcloud_quality_gate_badge():
    text = README.read_text(encoding="utf-8")
    assert "sonarcloud.io/api/project_badges/measure?project=jooservices_ai-flow&metric=alert_status" in text
    assert "sonarcloud.io/summary/new_code?id=jooservices_ai-flow" in text


def test_sonarcloud_is_documented_with_admin_import_and_topic_link():
    doc = DOC.read_text(encoding="utf-8")
    assert "SonarCloud" in doc
    assert "jooservices_ai-flow" in doc
    assert "organization admin" in doc.casefold() or "org admin" in doc.casefold() or "one-time" in doc.casefold()
    assert "SONAR_TOKEN" in doc
    assert "secret" in doc.casefold()
    assert "do not print" in doc.casefold() or "never print" in doc.casefold() or "not printed" in doc.casefold()
    topic = TOPIC.read_text(encoding="utf-8").strip()
    assert topic.startswith("- [")
    assert "sonarcloud.md" in topic
    assert "CLI" not in doc or "developer" in doc.casefold()


def test_sonar_job_is_pipeline_only_in_the_catalog():
    from aiflow.v4.core import pipeline_checks

    assert pipeline_checks.unclassified_jobs(CI_TEXT) == []
    _, pipeline_only = pipeline_checks.project_catalog("ai-flow")
    sonar = next(row for row in pipeline_only if row["job"] == "sonar")
    assert sonar["name"] == "SonarCloud"
    assert "SonarCloud" in sonar["reason"] or "organization" in sonar["reason"].casefold()
