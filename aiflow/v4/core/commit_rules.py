"""Header rules for commit messages and pull request titles, and a normalizer that always satisfies them.

The rules mirror ``.github/commitlint.config.mjs`` (type list, lower-case type, non-empty subject, header of 10 to
120 characters, no trailing full stop, subject not starting in lower case) and the pull request title pattern of
``.github/workflows/semantic-pr.yml`` (the subject starts with an uppercase ASCII letter). A fixture of headers checked
against the real commitlint keeps this module honest (``tests/v4/test_commit_rules.py``).
"""
from __future__ import annotations

import re

TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "build", "ci", "chore", "revert")
HEADER_MIN = 10
HEADER_MAX = 120
TITLE_TARGET = 100  # normalized titles stay below the hard limit so a suffix added later cannot break it
_HEADER = re.compile(r"^(?P<type>\w*)(?:\((?P<scope>[^)]*)\))?!?: (?P<subject>.*)$")
_PR_SUBJECT = re.compile(r"^[A-Z].+$")
_TRAILING = " .,;:!?-–—"
_DANGLING = frozenset((
    "a", "an", "the", "to", "of", "and", "or", "for", "in", "on",
    "by", "with", "from", "at", "as", "is", "are", "that", "which", "into",
    "than", "its", "their", "this", "these", "those", "each", "every", "any", "some",
    "your", "our", "such", "be", "been", "not", "no",
))


def violations(header: str, *, pr_title: bool = False) -> list[str]:
    """Rule violations of one header line; an empty list means the header is accepted."""
    header = header.rstrip("\r\n")
    found: list[str] = []
    if not HEADER_MIN <= len(header) <= HEADER_MAX:
        found.append(f"header must be {HEADER_MIN} to {HEADER_MAX} characters, it has {len(header)}")
    parsed = _HEADER.match(header)
    if not parsed:
        return found + ["header must look like `type: Subject` or `type(scope): Subject`"]
    kind, subject = parsed["type"], parsed["subject"]
    if not kind:
        found.append("type is empty")
    elif kind != kind.lower():
        found.append("type must be lower case")
    elif kind not in TYPES:
        found.append(f"type must be one of {', '.join(TYPES)}")
    if not subject.strip():
        found.append("subject is empty")
        return found
    if subject.endswith("."):
        found.append("subject must not end with a full stop")
    if subject[0].islower():
        found.append("subject must not start with a lower-case letter")
    if pr_title and not _PR_SUBJECT.match(subject):
        found.append("pull request subject must start with an uppercase ASCII letter")
    return found


def message_violations(message: str) -> list[str]:
    """Violations of a full commit message: only its first line is ruled by the repository."""
    lines = message.splitlines()
    return violations(lines[0] if lines else "")


def normalize_title(kind: str, key: str, summary: str, limit: int = TITLE_TARGET) -> str:
    """A pull request title from the change kind, the ticket key and the Jira summary that always passes the rules.

    An ASCII-capital subject keeps the key at the end (``feat: Subject (KEY-1)``); any other subject starts with the key
    (``feat: KEY-1: subject``) because the pull request pattern needs an uppercase ASCII first letter.
    """
    kind = kind if kind in TYPES else "feat"
    text = " ".join(summary.split()).rstrip(_TRAILING)
    if not text:
        return _checked(f"{kind}: Change {key}", kind, key)
    text = text[0].upper() + text[1:]
    if re.match(r"^[A-Z]", text):
        suffix = f" ({key})"
        room = limit - len(f"{kind}: ") - len(suffix)
        cut = _cut(text, room)
        return _checked(f"{kind}: {cut}{suffix}", kind, key) if cut else f"{kind}: Change {key}"
    prefix = f"{kind}: {key}: "
    cut = _cut(text, limit - len(prefix))
    return _checked(prefix + cut, kind, key) if cut else f"{kind}: Change {key}"


def _cut(text: str, room: int) -> str:
    """Longest prefix of ``text`` within ``room`` characters that ends on a word boundary, not on a dangling word."""
    room = max(room, 1)
    if len(text) <= room:
        return text
    head = text[:room + 1]
    if " " not in head[1:]:
        return ""  # one overlong word (an identifier, a URL): never cut inside it, the caller falls back to a plain title
    head = head.rsplit(" ", 1)[0][:room].rstrip(_TRAILING)
    words = head.split(" ")
    while len(words) > 1 and words[-1].lower().rstrip(_TRAILING) in _DANGLING:
        words.pop()
    return " ".join(words).rstrip(_TRAILING)


def _checked(title: str, kind: str, key: str) -> str:
    return title if not violations(title, pr_title=True) else f"{kind}: Change {key}"


def check_message_file(path: str) -> int:
    """Hook entry: 0 when the first line of the commit message file is accepted (merges and comments are skipped)."""
    with open(path, encoding="utf-8") as handle:
        lines = [line for line in handle.read().splitlines() if not line.startswith("#")]
    header = next((line for line in lines if line.strip()), "")
    if header.startswith(("Merge ", "Revert \"", "fixup! ", "squash! ")):
        return 0
    problems = violations(header)
    if not problems:
        return 0
    print(f"Commit message rejected: {header!r}")
    for problem in problems:
        print(f"  - {problem}")
    print("Format: `type: Subject` or `type(scope): Subject (KEY-1)`; type is one of " + ", ".join(TYPES) +
          "; the subject starts with a capital letter, has no full stop at the end and the header stays within "
          f"{HEADER_MAX} characters.")
    return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(check_message_file(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == "check-message" else 2)
