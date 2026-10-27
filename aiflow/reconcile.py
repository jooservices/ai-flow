"""Read-only comparison of Jira and git: tickets whose work is already merged but whose Jira status is not Done.

    bin/aiflow-reconcile [--repo PATH --keys JAIF] [--base origin/develop]

A ticket key in a commit subject or the Jira: KEY field in merged pull-request metadata means the work was merged. A ticket
that Jira still shows in another status is listed. Nothing is changed in Jira or git."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from . import audit_common as common
from .jira import Jira

SUBJECT_KEY = re.compile(r"\(([A-Z][A-Z0-9]+-\d+)\)\s*$")
JIRA_FIELD = re.compile(r"^\s*Jira:\s*([A-Z][A-Z0-9]+-\d+)\s*$", re.IGNORECASE)
MERGE_SOURCE = re.compile(r"^Merge pull request #\d+ from [^/]+/(.+)$")
BRANCH_KEY = re.compile(r"^([A-Z][A-Z0-9]+-\d+)(?:[-_/]|$)")
MAX_KEYS_PER_QUERY = 100


def pull_request_keys(pull_requests: list[dict]) -> set[str]:
    keys = set()
    for pull_request in pull_requests:
        body = pull_request.get("body")
        explicit = set()
        if isinstance(body, str):
            explicit = {match.group(1).upper() for line in body.splitlines()
                        if (match := JIRA_FIELD.fullmatch(line))}
        if explicit:
            keys.update(explicit)
            continue
        title = pull_request.get("title", "")
        match = SUBJECT_KEY.search(title.strip()) if isinstance(title, str) else None
        if match:
            keys.add(match.group(1))
            continue
        branch = pull_request.get("headRefName", "")
        head = pull_request.get("head")
        if not branch and isinstance(head, dict):
            branch = head.get("ref", "")
        if isinstance(branch, str) and (match := BRANCH_KEY.match(branch)):
            keys.add(match.group(1))
    return keys


def github_repository(repo: Path, runner=subprocess.run) -> str:
    try:
        result = runner(["git", "-C", str(repo), "remote", "get-url", "origin"],
                        capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"cannot read GitHub origin: {error.__class__.__name__}") from error
    remote = result.stdout.strip().removesuffix(".git").rstrip("/")
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+)$", remote)
    if result.returncode != 0 or not match:
        raise RuntimeError("origin must be a GitHub repository")
    return f"{match.group(1)}/{match.group(2)}"


def github_base_branch(base: str) -> str:
    branch = base.removeprefix("refs/remotes/").removeprefix("refs/heads/")
    for remote in ("origin/", "upstream/"):
        if branch.startswith(remote):
            branch = branch[len(remote):]
            break
    if (not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith("/")
            or branch.endswith("/") or "//" in branch or ".." in branch
            or re.fullmatch(r"[0-9a-fA-F]{7,40}", branch)):
        raise ValueError("--base must name a GitHub branch, not a revision")
    return branch


def merged_pull_requests(repo: Path, base: str, runner=subprocess.run) -> list[dict]:
    """Read every merged pull request for the base branch through paginated GitHub API results."""
    repository = github_repository(repo, runner)
    branch = quote(github_base_branch(base), safe="")
    endpoint = f"repos/{repository}/pulls?state=closed&base={branch}&per_page=100"
    try:
        result = runner(["gh", "api", "--paginate", "--slurp", endpoint],
                        capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"cannot read merged pull-request metadata: {error.__class__.__name__}") from error
    if result.returncode != 0:
        detail = result.stderr.strip()[:200] or "GitHub API request failed"
        raise RuntimeError(f"cannot read merged pull-request metadata: {detail}")
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("GitHub pull-request metadata was not valid JSON") from error
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise RuntimeError("GitHub pull-request metadata did not contain paginated results")
    return [pull_request for page in pages for pull_request in page
            if isinstance(pull_request, dict) and pull_request.get("merged_at")]


def merged_keys(subjects: list[str], pull_requests: list[dict] | None = None) -> set[str]:
    keys = set()
    for subject in subjects:
        subject = subject.strip()
        match = SUBJECT_KEY.search(subject)
        if match:
            keys.add(match.group(1))
        merge = MERGE_SOURCE.match(subject)
        if merge and (branch_key := BRANCH_KEY.match(merge.group(1))):
            keys.add(branch_key.group(1))
    keys.update(pull_request_keys(pull_requests or []))
    return keys


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
    parser.add_argument("--keys", help="comma separated Jira project keys")
    parser.add_argument("--base", default="origin/develop", help="GitHub base branch to audit")
    args = parser.parse_args(argv)
    default_repo = Path(__file__).resolve().parent.parent
    repo = (args.repo or default_repo).expanduser().resolve()
    if args.keys is None:
        if repo != default_repo.resolve():
            parser.error("--keys is required when --repo points to another repository")
        keys = ["JAIF"]
    else:
        raw_keys = [key.strip() for key in args.keys.split(",") if key.strip()]
        invalid = [key for key in raw_keys if not re.fullmatch(r"[A-Z][A-Z0-9]+", key)]
        if invalid:
            parser.error(f"invalid Jira project key(s): {', '.join(invalid)}")
        if not raw_keys:
            parser.error("--keys needs at least one project key")
        keys = raw_keys
    try:
        pull_requests = merged_pull_requests(repo, args.base)
        merged = merged_keys(git_subjects(repo, args.base), pull_requests)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"cannot complete the merge audit: {error}", file=sys.stderr)
        return 2
    root = common.workspaces_root()
    issues = search_merged_issues(Jira(root), merged, keys)
    rows = mismatches(merged, issues)
    for row in rows:
        print(f"{row['key']:<10} {row['status']:<14} merged in {args.base}: {row['summary'][:80]}")
    print(f"\n{len(rows)} ticket(s) merged in git but not Done in Jira")
    return 1 if rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
