"""Server-side source inventory and secret-safe result handling for the administrator log viewer."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..core.log_sources import MAX_LOG_LINE_CHARS, MAX_LOG_LINES, sources_for_node
from . import support_bundle

REDACTION_MARKER = "[REDACTED]"
_REDACTION_VARIANTS = re.compile(r"\[REDACTED(?:_[A-Z]+)?\]")


def source_catalog(ctx) -> list[dict[str, Any]]:
    """Build the only source list the API can queue, from active registered nodes and known accounts."""
    nodes = ctx.db.all("SELECT name,role,status,token_hash,last_heartbeat FROM nodes"
                       " WHERE removed_at IS NULL ORDER BY name")
    accounts = list(ctx.repo().accounts)
    now = datetime.now(timezone.utc)
    sources: list[dict[str, Any]] = []
    for node in nodes:
        heartbeat = node.get("last_heartbeat")
        online = bool(node.get("status") == "online" and node.get("token_hash") and heartbeat
                      and (now - heartbeat).total_seconds() <= 75)
        sources.extend(sources_for_node(str(node["name"]), str(node.get("role") or ""), accounts, online))
    return sources


def find_source(ctx, source: str) -> dict[str, Any] | None:
    """Resolve a public source id only when it appears in the current API inventory."""
    return next((item for item in source_catalog(ctx) if item["id"] == source), None)


def redact_log_lines(value: Any) -> list[str]:
    """Reuse the support-bundle credential sanitizer and use one stable marker for all secret classes."""
    if not isinstance(value, list):
        return []
    lines = []
    for item in value[:MAX_LOG_LINES]:
        if not isinstance(item, str):
            continue
        sanitized = support_bundle.sanitize_text(item[:MAX_LOG_LINE_CHARS], names=False)
        lines.append(_REDACTION_VARIANTS.sub(REDACTION_MARKER, sanitized))
    return lines


def safe_node_result(result: dict[str, Any], status: str) -> dict[str, Any]:
    """Store only bounded, redacted log output; discard arbitrary node result fields and error details."""
    if status != "completed" or not isinstance(result, dict):
        return {"available": False, "lines": []}
    available = result.get("available") is True
    return {"available": available, "lines": redact_log_lines(result.get("lines")) if available else []}
