"""The CI jobs that the local gate runs and the jobs that remain pipeline-only.

`tests/v4/test_pipeline_checks.py` compares this catalog with `.github/workflows/ci.yml` so a new
job cannot silently escape the checks shown to project owners.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineJob:
    key: str
    name: str
    check_steps: tuple[str, ...] = ()
    pipeline_only_reason: str = ""
    name_prefix: bool = False
    command_markers: tuple[str, ...] = ()


PIPELINE_JOBS = (
    PipelineJob("test", "Test (Unit)", ("compile", "unit"), command_markers=("unittest", "compileall")),
    PipelineJob("security", "Security scan", pipeline_only_reason=(
        "Requires Docker to scan the repository and the built control image.")),
    PipelineJob("docs", "Documentation", ("docs-build", "docs-check"), command_markers=("docsite",)),
    PipelineJob("giant-files", "Giant files", ("giant-files",), command_markers=("giant_files", "giant-files")),
    PipelineJob("duplicate-blocks", "Duplicate blocks", ("duplicate-blocks",),
                command_markers=("duplicate_blocks", "duplicate-blocks")),
    PipelineJob("broad-except", "Broad exceptions", ("broad-except",),
                command_markers=("broad_except", "broad-except")),
    PipelineJob("sql-boundary", "SQL boundary", ("sql-boundary",),
                command_markers=("sql_boundary", "sql-boundary")),
    PipelineJob("quality", "Code quality", ("quality",), command_markers=("quality_checks",)),
    PipelineJob("architecture", "Architecture", ("architecture",), command_markers=("test_architecture.py",)),
    PipelineJob("coverage", "Coverage", pipeline_only_reason=(
        "Requires Redis and PostgreSQL, CI-only coverage tools, and a Codecov upload for the pull-request diff gate.")),
    PipelineJob("v4-shard", "Test (v4) shard", ("v4",), name_prefix=True, command_markers=("tests/v4",)),
    PipelineJob("changes", "Changed paths", pipeline_only_reason=(
        "Compares the pull request with its base commit, which only the pull-request checkout provides.")),
    PipelineJob("v4-browser", "Test (v4) browser journeys", pipeline_only_reason=(
        "Requires provisioned Playwright Chromium plus isolated PostgreSQL and Redis services.")),
    PipelineJob("v4-scale", "Scale proof (not required)", ("scale-proof",),
                command_markers=("test_scale_proof.py",)),
    PipelineJob("v4", "Test (v4)", pipeline_only_reason=(
        "GitHub's required-check aggregator; the local gate runs its test shard directly.")),
    PipelineJob("acceptance-fast", "Clean-install acceptance (fast)", pipeline_only_reason=(
        "Requires privileged Docker containers and clean control and node hosts.")),
    PipelineJob("acceptance-full", "Clean-install acceptance (full, nightly)", pipeline_only_reason=(
        "Requires privileged Docker containers and published release artifacts.")),
    PipelineJob("acceptance-bug", "Open a bug ticket for the failed nightly run", pipeline_only_reason=(
        "Runs only after the nightly acceptance failure and needs GitHub issue-write permission.")),
)


def unclassified_jobs(workflow_text: str) -> list[str]:
    """Workflow job keys missing from the local-check or pipeline-only catalog."""
    import yaml

    workflow = yaml.safe_load(workflow_text) or {}
    job_keys = set((workflow.get("jobs") or {}).keys())
    classified = {item.key for item in PIPELINE_JOBS}
    return sorted(job_keys - classified)


def catalog_errors() -> list[str]:
    """Return malformed catalog entries, including jobs assigned to both or neither side."""
    errors = []
    seen = set()
    for item in PIPELINE_JOBS:
        if item.key in seen:
            errors.append(f"duplicate workflow job {item.key}")
        seen.add(item.key)
        if bool(item.check_steps) == bool(item.pipeline_only_reason):
            errors.append(f"{item.key} needs either local check steps or a pipeline-only reason")
    return errors


def project_catalog(local_gate: object) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Return the named project checks and pipeline-only jobs for the built-in AI Flow gate."""
    if local_gate != "ai-flow":
        return [], []
    checks = [{"job": item.key, "name": item.name, "steps": list(item.check_steps)}
              for item in PIPELINE_JOBS if item.check_steps]
    pipeline_only = [{"job": item.key, "name": item.name, "reason": item.pipeline_only_reason}
                     for item in PIPELINE_JOBS if item.pipeline_only_reason]
    return checks, pipeline_only


def failure_gaps(job_names: list[str], local_gate: object, checks: list[str] | None = None) -> list[dict[str, str]]:
    """Describe failed pipeline jobs that the developer's configured local gate did not run."""
    gaps = []
    for name in job_names:
        item = next((entry for entry in PIPELINE_JOBS if _matches_job_name(name, entry)), None)
        if item and item.check_steps and _check_was_configured(item, local_gate, checks or []):
            continue
        reason = (item.pipeline_only_reason if item and item.pipeline_only_reason else
                  "This job is not covered by the project's built-in local gate.")
        gaps.append({"job": name, "reason": reason})
    return gaps


def _check_was_configured(item: PipelineJob, local_gate: object, checks: list[str]) -> bool:
    if local_gate == "ai-flow":
        return True
    configured = list(checks)
    if isinstance(local_gate, (list, tuple)):
        configured.extend(str(command) for command in local_gate)
    return any(marker in check.casefold() for check in configured for marker in item.command_markers)


def _matches_job_name(name: str, item: PipelineJob) -> bool:
    normalized = name.strip().casefold()
    expected = item.name.casefold()
    return normalized == expected or normalized.endswith("/" + expected) or (
        item.name_prefix and normalized.startswith(expected))
