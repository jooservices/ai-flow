"""Context-based ticket helpers shared by control routes."""

from __future__ import annotations

from typing import Any

from .features.tickets import ticket_repository_of


def ticket_epic(ctx: Any, ticket: dict[str, Any]) -> str:
    """Resolve a ticket's parent chain to its Jira epic key."""
    if ticket.get("type") == "Epic":
        return str(ticket.get("key") or "")
    parent = str(ticket.get("parent_key") or "")
    seen: set[str] = set()
    while parent and parent not in seen:
        seen.add(parent)
        ancestor = ticket_repository_of(ctx).ticket_ancestor(parent)
        if not ancestor:
            return parent
        if ancestor["type"] == "Epic":
            return parent
        parent = str(ancestor.get("parent_key") or "")
    return ""
