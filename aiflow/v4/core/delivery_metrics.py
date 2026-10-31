"""Delivery metrics from merged pull requests: lead time, branch-update commits, CI attempts and hot-file churn.

`python3 -m aiflow.v4.core.delivery_metrics [--limit 100]` prints them as markdown (docs/v4/delivery-metrics.md). The
refactor of epic JAIF-445 is judged against the baseline recorded there. The calculation is pure; only `fetch` calls gh."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from datetime import datetime
from typing import Any

REPO = "jooservices/ai-flow"
HOT_FILES = ("aiflow/v4/control/api.py", "aiflow/v4/control/ui/console/")  # a trailing slash counts a folder
UPDATE_PREFIXES = ("Merge branch", "Merge remote-tracking branch")


def _is_file(path: Any, name: str) -> bool:
    return path == name or (name.endswith("/") and str(path or "").startswith(name))


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def compute(prs: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """`prs` are `gh pr list --json` rows of merged pull requests; `runs` are workflow runs (head_branch, run_attempt)."""
    lead_hours = [(_time(p["mergedAt"]) - _time(p["createdAt"])).total_seconds() / 3600 for p in prs]
    updates = [sum(1 for c in p.get("commits", []) if str(c.get("messageHeadline", "")).startswith(UPDATE_PREFIXES))
               for p in prs]
    attempts: dict[str, int] = {}
    for run in runs:
        attempts[run["head_branch"]] = attempts.get(run["head_branch"], 0) + int(run.get("run_attempt") or 1)
    ci = [attempts[p["headRefName"]] for p in prs if p.get("headRefName") in attempts]
    touched = {name: sum(1 for p in prs if any(_is_file(f.get("path"), name) for f in p.get("files", [])))
               for name in HOT_FILES}
    count = len(prs)
    return {
        "pull_requests": count,
        "lead_time_hours": {"median": round(statistics.median(lead_hours), 2) if lead_hours else 0.0,
                            "p90": round(percentile(lead_hours, 90), 2)},
        "prs_with_branch_update_commits": sum(1 for n in updates if n),
        "branch_update_commits_per_pr": round(sum(updates) / count, 2) if count else 0.0,
        "ci_attempts_per_pr": round(sum(ci) / len(ci), 2) if ci else 0.0,
        "hot_file_prs": touched,
        "hot_file_share": {name: round(n / count, 2) if count else 0.0 for name, n in touched.items()},
    }


def render(metrics: dict[str, Any], as_of: str) -> str:
    lines = [f"Delivery metrics as of {as_of} over {metrics['pull_requests']} merged pull requests:", "",
             "| Metric | Value |", "| --- | --- |",
             f"| Lead time, median (hours) | {metrics['lead_time_hours']['median']} |",
             f"| Lead time, p90 (hours) | {metrics['lead_time_hours']['p90']} |",
             f"| Pull requests with a branch-update commit | {metrics['prs_with_branch_update_commits']} |",
             f"| Branch-update commits per pull request | {metrics['branch_update_commits_per_pr']} |",
             f"| CI attempts per pull request | {metrics['ci_attempts_per_pr']} |"]
    for name, n in metrics["hot_file_prs"].items():
        lines.append(f"| Pull requests touching `{name}` | {n} ({round(metrics['hot_file_share'][name] * 100)}%) |")
    return "\n".join(lines) + "\n"


def _gh(*args: str) -> Any:
    return json.loads(subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout)


def fetch(limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merged pull requests newest first, one `gh pr view` each (one list query for all of them is too large for GraphQL)."""
    numbers = [row["number"] for row in _gh("pr", "list", "-R", REPO, "--state", "merged", "--base", "develop",
                                            "--limit", str(limit), "--json", "number")]
    prs = [_gh("pr", "view", str(n), "-R", REPO, "--json", "number,createdAt,mergedAt,headRefName,commits,files")
           for n in numbers]
    runs: list[dict[str, Any]] = []
    for page in range(1, 6):
        data = _gh("api", f"repos/{REPO}/actions/runs?event=pull_request&per_page=100&page={page}")
        runs.extend(data.get("workflow_runs", []))
        if len(data.get("workflow_runs", [])) < 100:
            break
    return prs, [r for r in runs if r.get("name") == "CI"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    prs, runs = fetch(args.limit)
    print(render(compute(prs, runs), datetime.now().date().isoformat()), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
