"""What may enter the memory, and how a free text becomes a safe search."""
from __future__ import annotations

import hashlib
import re

KINDS = ("fact", "case", "lesson", "context")
SCOPES = ("global", "namespace", "project", "ticket")
STATUSES = ("fresh", "verified", "promoted", "decayed")
MAX_TITLE, MAX_BODY, MAX_TAGS, MAX_TAG = 200, 8000, 12, 40
STOP = frozenset("the and for with that this from into when then than are was were been have has had not but all any can will should must "
                 "ticket task tests test code file files work use used using also each more only same some such via per its our out".split())

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


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def content_hash(kind: str, title: str, body: str) -> str:
    return hashlib.sha256(f"{kind}\0{normalise(title)}\0{normalise(body)}".encode()).hexdigest()


def check_entry(entry: dict) -> dict:
    """Validate and clean a proposed entry; raises Rejected."""
    kind, scope = entry.get("kind", ""), entry.get("scope", "")
    if kind not in KINDS:
        raise Rejected(f"kind must be one of {', '.join(KINDS)}")
    if scope not in SCOPES:
        raise Rejected(f"scope must be one of {', '.join(SCOPES)}")
    title, body = str(entry.get("title", "")).strip(), str(entry.get("body", "")).strip()
    if not 3 <= len(title) <= MAX_TITLE:
        raise Rejected(f"title must be 3 to {MAX_TITLE} characters")
    if not 3 <= len(body) <= MAX_BODY:
        raise Rejected(f"body must be 3 to {MAX_BODY} characters")
    tags = entry.get("tags") or []
    if not isinstance(tags, list) or len(tags) > MAX_TAGS or not all(isinstance(t, str) and 0 < len(t) <= MAX_TAG for t in tags):
        raise Rejected(f"tags must be a list of at most {MAX_TAGS} short texts")
    namespace, project = str(entry.get("namespace", "")), str(entry.get("project", ""))
    if scope == "global":
        namespace = project = ""
    elif scope == "namespace":
        project = ""
    if (scope in ("namespace", "project", "ticket") and not namespace) or (scope in ("project", "ticket") and not project):
        raise Rejected(f"scope {scope} needs a namespace" + (" and a project" if scope in ("project", "ticket") else ""))
    for name, pattern in SECRETS.items():
        if pattern.search(title) or pattern.search(body):
            raise Rejected(f"looks like it contains a secret ({name}); remove it and propose again")
    return dict(kind=kind, scope=scope, namespace=namespace, project=project, repo=str(entry.get("repo", "")), jira_key=str(entry.get("jira_key", "")),
                title=title, body=body, tags=[t.strip().lower() for t in tags], source=entry.get("source") if isinstance(entry.get("source"), dict) else {},
                created_by=str(entry.get("created_by", ""))[:80], supersedes=entry.get("supersedes") or None,
                confidence=float(entry.get("confidence", 0.5)), expires_at=entry.get("expires_at") or None)


def tsquery(text: str, limit: int = 24) -> str:
    """`word | word | ...` from free text: letters and digits only (so it is safe to hand to to_tsquery), common words dropped."""
    seen: list[str] = []
    for word in re.findall(r"[\w]{3,}", text.lower()):
        if word not in STOP and word not in seen and not word.isdigit():
            seen.append(word)
        if len(seen) >= limit:
            break
    return " | ".join(seen)
