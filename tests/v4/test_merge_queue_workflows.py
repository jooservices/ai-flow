"""Keep workflows that provide required checks enabled for GitHub merge queues."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (
    ROOT / ".github/workflows/ci.yml",
    ROOT / ".github/workflows/commitlint.yml",
    ROOT / ".github/workflows/semantic-pr.yml",
)


def _allows_event(condition: str | bool, event_name: str) -> bool:
    if isinstance(condition, bool):
        return condition

    expression = re.sub(r"\$\{\{\s*(.*?)\s*}}", r"\1", condition, flags=re.DOTALL)
    expression = re.sub(
        r"github\.event_name\s*(==|!=)\s*(['\"])([^'\"]+)\2",
        lambda match: str((event_name == match.group(3)) == (match.group(1) == "==")),
        expression,
    )
    expression = re.sub(r"\balways\(\)", "True", expression)
    expression = re.sub(r"\bfailure\(\)", "False", expression)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    tree = ast.parse(expression.strip(), mode="eval")

    def evaluate(node: ast.expr) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BoolOp):
            values = [evaluate(value) for value in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
        raise AssertionError(f"Unsupported job condition expression: {condition!r}")

    return evaluate(tree.body)


def _triggers(workflow: dict) -> dict:
    # PyYAML's YAML 1.1 resolver parses the unquoted key `on` as boolean True.
    return workflow["on"] if "on" in workflow else workflow[True]


def test_required_check_workflows_run_for_merge_group_events():
    for path in WORKFLOWS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = _triggers(workflow)
        assert "merge_group" in triggers, f"{path.name} does not trigger on merge_group"
        merge_group = triggers["merge_group"]
        assert not isinstance(merge_group, dict) or "branches" not in merge_group

        for job_name, job in workflow["jobs"].items():
            if "if" not in job:
                continue
            condition = job["if"]
            if _allows_event(condition, "pull_request"):
                assert _allows_event(condition, "merge_group"), (
                    f"{path.name} job {job_name!r} allows pull_request but excludes merge_group: "
                    f"{condition!r}"
                )


def test_pull_request_only_steps_are_skipped_on_merge_group_events():
    workflow = yaml.safe_load((ROOT / ".github/workflows/commitlint.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["commitlint"]["steps"]
    step = next(step for step in steps if step.get("uses", "").startswith("wagoid/commitlint-github-action"))

    assert _allows_event(step["if"], "pull_request")
    assert not _allows_event(step["if"], "merge_group")
