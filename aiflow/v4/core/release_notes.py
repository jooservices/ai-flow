"""Small conventional-commit changelog renderer used by the tagged v4 release workflow."""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict

_HEADER = re.compile(r"^(feat|fix|perf|refactor|docs|build|ci|test|chore|revert)(?:\(([^)]+)\))?(!)?:\s*(.+)$")
_SECTIONS = {
    "feat": "Features",
    "fix": "Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "build": "Build",
    "ci": "CI",
    "test": "Tests",
    "chore": "Maintenance",
    "revert": "Reverts",
}


def render_changelog(commits: list[tuple[str, str, str]]) -> str:
    """Render (sha, subject, body) tuples as grouped conventional-commit release notes."""
    sections: dict[str, list[str]] = defaultdict(list)
    breaking: list[str] = []
    for sha, subject, body in commits:
        match = _HEADER.match(subject.strip())
        if not match:
            sections["Other"].append(f"- {subject.strip()} ({sha[:7]})")
            continue
        kind, scope, bang, summary = match.groups()
        description = f"**{scope}:** {summary}" if scope else summary
        entry = f"- {description} ({sha[:7]})"
        sections[_SECTIONS[kind]].append(entry)
        breaking_note = next((line.split(":", 1)[1].strip() for line in body.splitlines()
                              if line.startswith("BREAKING CHANGE:")), "")
        if bang or breaking_note:
            breaking.append(f"- {description} ({sha[:7]})" + (f" — {breaking_note}" if breaking_note else ""))

    order = ["Breaking Changes", "Features", "Fixes", "Performance", "Refactoring", "Documentation",
             "Build", "CI", "Tests", "Maintenance", "Reverts", "Other"]
    output = []
    for title in order:
        entries = breaking if title == "Breaking Changes" else sections.get(title, [])
        if entries:
            output.extend((f"## {title}", "", *entries, ""))
    return "\n".join(output).rstrip() + "\n"


def git_commits(previous: str | None, current: str) -> list[tuple[str, str, str]]:
    """Read commits since the previous tag (or all reachable commits for the first release)."""
    revision_range = f"{previous}..{current}" if previous else current
    result = subprocess.run(["git", "log", "--no-merges", "--format=%H%x00%s%x00%b%x00",
                             revision_range], check=True, capture_output=True, text=True)
    parts = result.stdout.split("\0")
    return [(parts[i].strip(), parts[i + 1], parts[i + 2])
            for i in range(0, len(parts) - 2, 3) if parts[i].strip()]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Render release notes from conventional commits")
    parser.add_argument("current", help="current release tag")
    parser.add_argument("--previous", default="", help="previous release tag (omit for the first release)")
    args = parser.parse_args()
    print(render_changelog(git_commits(args.previous or None, args.current)), end="")


if __name__ == "__main__":
    main()
