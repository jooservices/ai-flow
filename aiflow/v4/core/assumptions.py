"""Pure helper functions for parsing and formatting ticket assumptions."""
from __future__ import annotations

import re

ASSUMPTIONS_BLOCK = re.compile(
    r"^[ \t]*Assumptions:[ \t]*\n((?:[ \t]*[-*].*(?:\n|$))+)",
    re.M | re.I,
)
EXPLICIT_WHY = re.compile(r"[;—–,]?\s*\b(?:why|reason):\s*", re.I)
LOOSE_WHY = re.compile(r"\s+[—–]\s+|;\s+")  # a dash splits only when spaces surround it: "10–20" stays whole
CONTRADICTED_LINE = re.compile(
    r"^[ \t]*[-*][ \t]*(?:\d+\.[ \t]*)?(?:F\d+:[ \t]*)?assumption:[ \t]*(.+?)[ \t]+contradicts\b", re.I | re.M)


def parse_assumptions(text: str) -> list[dict[str, str]]:
    """Extract bulleted assumptions from ticket text under an Assumptions heading."""
    if not text:
        return []
    match = ASSUMPTIONS_BLOCK.search(text)
    if not match:
        return []
    raw_lines = [
        re.sub(r"^[ \t]*[-*][ \t]*", "", line).strip()
        for line in match.group(1).splitlines()
        if line.strip()
    ]
    out: list[dict[str, str]] = []
    for line in raw_lines:
        if line.casefold() in {"none", "(none)", "none."}:
            continue
        parts = (EXPLICIT_WHY if EXPLICIT_WHY.search(line) else LOOSE_WHY).split(line, maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            out.append({"assumption": parts[0].strip(), "why": parts[1].strip()})
        else:
            out.append({"assumption": line, "why": ""})
    return out


def format_assumptions(assumptions: list[dict[str, str]]) -> str:
    """Format a list of assumption dictionaries into an Assumptions markdown block."""
    if not assumptions:
        return ""
    lines = ["Assumptions:"]
    for item in assumptions:
        what = item.get("assumption", "").strip()
        why = item.get("why", "").strip()
        if why:
            lines.append(f"- {what}; why: {why}")
        else:
            lines.append(f"- {what}")
    return "\n".join(lines)


def contradicted_assumption_texts(findings: str) -> list[str]:
    """The assumptions a verifier named as contradicting a rule (`- assumption: X contradicts Y: why`)."""
    return [match.group(1).strip() for match in CONTRADICTED_LINE.finditer(findings or "")]


def invalid_assumption_items(items: object) -> list[str]:
    """Reasons an assumptions payload cannot be stored; an empty list means every item is valid."""
    if not isinstance(items, list):
        return ["assumptions must be a list"]
    return [f"assumption {index + 1} needs a non-empty text and a text reason" for index, item in enumerate(items)
            if not isinstance(item, dict) or not isinstance(item.get("assumption"), str)
            or not item["assumption"].strip() or not isinstance(item.get("why", ""), str)]
