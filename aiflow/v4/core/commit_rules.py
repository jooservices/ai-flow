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
_DANGLING = frozenset(("a an the to of and or for in on by with from at as is are that which into than its their this "
                      "these those each every any some your our such be been not no").split())


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
        return _checked(f"{kind}: {_cut(text, room)}{suffix}", kind, key)
    prefix = f"{kind}: {key}: "
    return _checked(prefix + _cut(text, limit - len(prefix)), kind, key)


def _cut(text: str, room: int) -> str:
    """Longest prefix of ``text`` within ``room`` characters that ends on a word boundary, not on a dangling word."""
    room = max(room, 1)
    if len(text) <= room:
        return text
    head = text[:room + 1]
    head = head.rsplit(" ", 1)[0] if " " in head[1:] else text[:room]
    head = head[:room].rstrip(_TRAILING)
    words = head.split(" ")
    while len(words) > 1 and words[-1].lower().rstrip(_TRAILING) in _DANGLING:
        words.pop()
    return " ".join(words).rstrip(_TRAILING) or text[:room]


def _checked(title: str, kind: str, key: str) -> str:
    return title if not violations(title, pr_title=True) else f"{kind}: Change {key}"
