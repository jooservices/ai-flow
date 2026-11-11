"""Shared secret screening for v4 and legacy memory callers."""
from __future__ import annotations

import re

SECRETS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "API key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "credentials in a URL": re.compile(r"://[^/\s:@]+:[^/\s@]{3,}@"),
    "password or token assignment": re.compile(r"(?i)\b(?:pass(?:word|wd)?|secret|token|api[_-]?key|private[_-]?key)\b\s*[:=]\s*[\"']?[^\s\"']{6,}"),
}


class Rejected(ValueError):
    """The entry may not be stored; the message never contains the offending value."""


def scan_secrets(*texts: str) -> None:
    """Raise Rejected when a text looks like it contains a secret (the message never contains the value)."""
    for name, pattern in SECRETS.items():
        if any(pattern.search(text) for text in texts):
            raise Rejected(f"looks like it contains a secret ({name}); remove it and propose again")
