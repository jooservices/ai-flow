"""What the supervisor writes into the memory by itself (no AI, no tokens) and how it shows recalled entries to an AI."""
from __future__ import annotations

import re
from typing import Any

MEMORY_LINE = re.compile(r"^MEMORY:\s*(fact|case|lesson)\s*\|\s*(.{3,200}?)\s*\|\s*(.{3,1500})$", re.M)
HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*$|^\*\*(.+?)\*\*\s*:?\s*$", re.M)
WANTED = ("what's changed", "whats changed", "why changed", "study cases", "study case")


def sections(text: str) -> dict[str, str]:
    """Hand-over comment -> {lower-case heading: text}."""
    found: dict[str, str] = {}
    name, lines = "", []
    for line in text.splitlines():
        match = HEADING.match(line.strip())
        if match:
            if name:
                found[name] = "\n".join(lines).strip()
            name, lines = (match.group(1) or match.group(2)).strip().lower().rstrip(":"), []
        elif name:
            lines.append(line)
    if name:
        found[name] = "\n".join(lines).strip()
    return found


def handover(comments: list[dict[str, Any]]) -> dict[str, str]:
    """The sections of the latest developer hand-over comment (the one that has `What's changed`)."""
    for comment in reversed(comments):
        parsed = sections(str(comment.get("body", "")))
        if any(key.startswith(w) for key in parsed for w in WANTED[:2]):
            return parsed
    return {}


def build_case(key: str, summary: str, company: str, repo: str, change_type: str, developer: str, pr_url: str, comments: list[dict[str, Any]],
               rework_rounds: int, rework_text: str = "") -> dict[str, Any] | None:
    """A `case` from a ticket that is merged and Done; None when the developer left no hand-over to learn from."""
    parts = handover(comments)

    def pick(*names: str) -> str:
        for name, text in parts.items():
            if any(name.startswith(n) for n in names) and text and text.lower() not in ("none", "n/a", "-"):
                return text
        return ""
    changed, why, study = pick("what's changed", "whats changed"), pick("why changed"), pick("study case")
    if not (changed or why or study):
        return None
    body = [f"Ticket {key}: {summary}", ""]
    if changed:
        body += ["What changed:", changed[:2200], ""]
    if why:
        body += ["Why:", why[:1500], ""]
    if study:
        body += ["Study cases / lessons:", study[:2200], ""]
    if rework_rounds:
        body += [f"Rework rounds: {rework_rounds}." + (f" Last reasons: {rework_text[:900]}" if rework_text else ""), ""]
    if pr_url:
        body.append(f"Pull request: {pr_url}")
    tags = [t for t in (key.rsplit("-", 1)[0].lower(), repo.lower(), change_type.lower(), developer.lower()) if t]
    return dict(kind="case", scope="project", namespace=company, project=key.rsplit("-", 1)[0], repo=repo, jira_key=key, title=summary[:200],
                body="\n".join(body).strip()[:7900], tags=tags, source={"jira": key, "pr": pr_url}, created_by="supervisor", status="verified", confidence=0.7)


def lessons(text: str, limit: int = 3) -> list[tuple[str, str, str]]:
    """`MEMORY: lesson | title | body` lines the AI printed in its final message -> [(kind, title, body)]."""
    return [(k, t, b) for k, t, b in MEMORY_LINE.findall(text)][:limit]


def prompt_block(items: list[dict[str, Any]]) -> str:
    """Recalled entries as a short markdown list for the prompt."""
    if not items:
        return "(nothing recorded yet for this work)"
    lines = []
    for item in items:
        where = item["project"] or item["namespace"] or "global"
        lines.append(f"- [{item['kind']}] {item['title']} ({where}, {item['status']}, id {str(item['id'])[:8]})")
        lines.append("  " + " ".join(str(item["body"]).split())[:420])
    return "\n".join(lines)
