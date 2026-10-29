"""Tools that a stopped run reports as missing in its own words (JAIF-368).

A developer that stops with "uv is absent" or "`uv: command not found`" has a machine problem, not a question for the
owner. The explicit ``MISSING-TOOL:`` marker is handled by :mod:`toolchains`; this module recognizes the same fact in
free text, but only for tools AI Flow knows by name (the node and project catalogs), so a sentence about something else
never turns into a machine requirement.
"""
from __future__ import annotations

import re

from .toolchains import known_requirements

_ABSENT = (r"(?:absent|missing|not installed|not available|unavailable|not found|cannot be found|could not be found|"
           r"not present|not on (?:the )?path|not in (?:the )?path)")
_PYTHON = re.compile(r"\bpython\s?3(?:\.\d+)?\b", re.I)


def _patterns(name: str) -> list[re.Pattern[str]]:
    n = re.escape(name)
    return [
        # "uv is absent", "`uv` is missing from the sandbox", "uv, which is not installed"
        re.compile(rf"(?<![\w.-]){n}(?![\w.-])[`'\"]?[^.\n;]{{0,40}}?\b{_ABSENT}\b", re.I),
        # "`uv: command not found`", "command not found: uv", "bash: uv: command not found"
        re.compile(rf"(?<![\w.-]){n}(?![\w.-])[`'\"]?:?\s*command not found|command not found:?\s*[`'\"]?{n}(?![\w.-])", re.I),
        # "no uv binary on this machine"
        re.compile(rf"\bno\s+[`'\"]?{n}(?![\w.-])[`'\"]?\s+(?:binary|executable|command|installation)\b", re.I),
    ]


def missing_tools(text: str) -> list[str]:
    """Catalog tool names the text reports as missing, in order of appearance in the catalog."""
    text = text or ""
    found = [name for name in known_requirements() if any(p.search(text) for p in _patterns(name))]
    if "python3" not in found and any(_PYTHON.search(m.group(0)) for m in re.finditer(
            rf"[^.\n;]{{0,60}}\b{_ABSENT}\b|command not found[^.\n;]{{0,40}}", text, re.I) if _PYTHON.search(m.group(0))):
        found.append("python3")
    return found
