"""Redact account identifiers embedded in free-text control responses."""
from __future__ import annotations

import re


_ACCOUNT_FIELDS = {"account", "account_id"}
_IDENTIFIER_CHARS = r"A-Za-z0-9_.-"


def redact_account_ids_in_text(data: dict) -> None:
    """Replace account IDs found in identity fields wherever they also occur in response text."""
    account_ids = sorted(_account_ids(data), key=len, reverse=True)
    if not account_ids:
        return
    patterns = [re.compile(rf"(?<![{_IDENTIFIER_CHARS}]){re.escape(account_id)}(?![{_IDENTIFIER_CHARS}])")
                for account_id in account_ids]
    _redact_text(data, patterns)


def _account_ids(value) -> set[str]:
    if isinstance(value, dict):
        found = {item for key, item in value.items()
                 if key in _ACCOUNT_FIELDS and isinstance(item, str) and item}
        for item in value.values():
            found.update(_account_ids(item))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found.update(_account_ids(item))
        return found
    return set()


def _redact_text(value, patterns: list[re.Pattern]):
    if isinstance(value, dict):
        for key, item in value.items():
            value[key] = _redact_text(item, patterns)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _redact_text(item, patterns)
    elif isinstance(value, str):
        for pattern in patterns:
            value = pattern.sub("Verifier", value)
    return value
