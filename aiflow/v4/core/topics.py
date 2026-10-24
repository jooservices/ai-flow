"""Topic index of the deploy documentation, generated so that tickets never edit the same line.

Every ticket used to append one line to the "Topics" list of deploy/v4/README.md, so two tickets that ran at the same
time always conflicted. Now each new topic is its own file, `deploy/v4/topics/<slug>.md`, holding exactly one line

    - [Title](../../../docs/v4/<topic>.md): what it covers

and the index is generated from those files (plus the lines the README already holds) by `index()`; run
`uv run python -m aiflow.v4.core.topics` to print it. The files never conflict with each other: adding a topic adds
a file. docs/v4/merge-conflicts.md describes the rule.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
README = ROOT / "deploy" / "v4" / "README.md"
TOPICS = ROOT / "deploy" / "v4" / "topics"
LINE = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<link>[^)]+)\)(?::\s*(?P<text>.*))?$")


def readme_lines(readme: Path = README) -> list[str]:
    """The topic lines the README itself holds (older topics)."""
    text = readme.read_text(encoding="utf-8")
    if "## Topics" not in text:
        return []
    section = text.split("## Topics", 1)[1].split("\n## ", 1)[0]
    return [line.rstrip() for line in section.splitlines() if LINE.match(line)]


def topic_lines(directory: Path = TOPICS) -> list[str]:
    """One line per topic file, sorted by file name. A file must hold exactly one topic line."""
    lines: list[str] = []
    for path in sorted(directory.glob("*.md")):
        found = [x.rstrip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        if len(found) != 1 or not LINE.match(found[0]):
            raise ValueError(f"{path.name}: a topic file holds exactly one line '- [Title](link): text'")
        lines.append(found[0])
    return lines


def index(readme: Path = README, directory: Path = TOPICS) -> str:
    """The whole topic list: topic files first (newest work), then the lines of the README. Links are made relative
    to deploy/v4/ so the list reads the same as in the README."""
    own = [line.replace("](../../../", "](../../") for line in topic_lines(directory)]
    return "\n".join(own + readme_lines(readme)) + "\n"


if __name__ == "__main__":
    sys.stdout.write(index())
