"""Read-only comparison of Jira and git: tickets whose work is already merged but whose Jira status is not Done.

    bin/aiflow-reconcile [--repo PATH --keys JAIF] [--base origin/develop]

A ticket key in a commit subject on the base branch (the supervisor writes `type(scope): Subject (KEY)`) means the work was merged. Such a ticket
that Jira still shows in another status is listed. Nothing is changed in Jira or git."""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from . import audit_common as common
from .jira import Jira

SUBJECT_KEY = re.compile(r"\(([A-Z][A-Z0-9]+-\d+)\)\s*$")
MAX_KEYS_PER_QUERY = 100


def merged_keys(subjects: list[str]) -> set[str]:
    return {m.group(1) for subject in subjects if (m := SUBJECT_KEY.search(subject.strip()))}


def mismatches(merged: set[str], issues: list[dict]) -> list[dict]:
    rows = []
    for issue in issues:
        fields = issue.get("fields")
        fields = fields if isinstance(fields, dict) else {}
        status = issue.get("status")
        if not isinstance(status, str):
            nested_status = fields.get("status")
            status = nested_status.get("name", "") if isinstance(nested_status, dict) else ""
        summary = issue.get("summary") or fields.get("summary", "")
        if issue.get("key") in merged and status not in ("Done", "Declined"):
            rows.append({"key": issue["key"], "status": status, "summary": summary})
    return sorted(rows, key=lambda row: row["key"])


def search_merged_issues(jira, merged: set[str], project_keys: list[str]) -> list[dict]:
    """Search the merged keys in bounded batches, avoiding a project-wide result cap."""
    allowed_projects = set(project_keys)
    candidates = sorted(key for key in merged if key.partition("-")[0] in allowed_projects)
    issues = []
    for start in range(0, len(candidates), MAX_KEYS_PER_QUERY):
        batch = candidates[start:start + MAX_KEYS_PER_QUERY]
        jql = f"key in ({', '.join(batch)}) AND status not in (Done, Declined)"
        issues.extend(jira.search(jql, limit=len(batch)))
    return issues


def git_subjects(repo: Path, base: str) -> list[str]:
    if base.startswith("-"):
        raise ValueError("--base must be a branch or revision, not an option")
    result = subprocess.run(["git", "-C", str(repo), "log", "--format=%s", "--end-of-options", base], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:200])
    return result.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Jira versus git comparison.")
    parser.add_argument("--repo", type=Path, help="repository to read (default: this one)")
    parser.add_argument("--keys", default="JAIF", help="comma separated Jira project keys")
    parser.add_argument("--base", default="origin/develop")
    args = parser.parse_args(argv)
    root = common.workspaces_root()
    repo = args.repo or Path(__file__).resolve().parent.parent
    keys = [k.strip() for k in args.keys.split(",") if re.fullmatch(r"[A-Z][A-Z0-9]+", k.strip())]
    if not keys:
        parser.error("--keys needs at least one project key")
    merged = merged_keys(git_subjects(repo, args.base))
    issues = search_merged_issues(Jira(root), merged, keys)
    rows = mismatches(merged, issues)
    for row in rows:
        print(f"{row['key']:<10} {row['status']:<14} merged in {args.base}: {row['summary'][:80]}")
    print(f"\n{len(rows)} ticket(s) merged in git but not Done in Jira")
    return 1 if rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
