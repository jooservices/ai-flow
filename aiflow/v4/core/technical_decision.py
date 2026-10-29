"""Technical choices that the SA role decides, never the owner (JAIF-369).

After a ticket is Ready, a question about a test, a refactor, a module, a name, a library or how to split a change is an
engineering decision. The owner decides requirement conflicts, irreversible or production actions, and money, security
and licensing; this module recognizes the rest so the escalation classifier can send it to the SA instead.
"""
from __future__ import annotations

import re

TECHNICAL_RE = re.compile(
    r"\b(?:test|tests|unit test|fixture|mock|refactor\w*|module|class|function|helper|structure|naming|library|"
    r"dependency|package|schema|migration|index|query|split|separate (?:change|ticket|pull request|pr|commit)|"
    r"order(?:ing)? of|follow-up|lint|format\w*|type hint|retry|cache|batch size|default value|abstraction|"
    r"interface|duplicate|rename)\b", re.I)
OWNER_RE = re.compile(
    r"\b(?:price|pricing|payment|billing|invoice|refund|cost|budget|money|secret|credential|password|token|api key|"
    r"security|vulnerab\w*|licen[cs]\w*|gdpr|privacy|personal data|legal|customer|contract|production|prod|"
    r"conflicts? with (?:AC\s?\d+|the (?:ticket|requirement|acceptance|spec))|requirement conflict|business)\b", re.I)


def is_technical(text: str) -> bool:
    """Whether the question is an engineering choice that does not touch an owner-only topic."""
    text = text or ""
    return bool(TECHNICAL_RE.search(text)) and not OWNER_RE.search(text)
