"""Per-person console layouts (docs/v4/console-layouts.md).

Every section of a console page that shows several sections is a *block* with a stable identifier (the
``data-block`` attribute in ``ui/console.html``). A person can reorder, resize, hide and reset the blocks of each page.
The layout is stored on the server (table ``console_layouts``, keyed by the person's email and the page), so it follows
the person to another browser or computer.

``PAGES`` is the single list of pages and blocks. A saved layout is always *resolved* against it:

* a block in the saved layout that is no longer in ``PAGES`` is ignored, without error;
* a block in ``PAGES`` that the saved layout does not mention (added by a later release) is appended at the end,
  visible, so a saved layout can never lose it;
* with nothing saved, the person gets the default of their role (``ROLE_PROFILE`` -> ``PROFILES``).

The role defaults are keyed by the roles of ``console_auth.ROLE_PERMISSIONS``. A role without a profile gets the
canonical layout, so adding a role never breaks the console.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from .console_auth import ROLE_PERMISSIONS

SIZES = ("small", "medium", "large", "full")
MAX_BLOCKS = 64

# page -> (title, [(block id, title, default size)]) in the canonical order.
PAGES: dict[str, tuple[str, list[tuple[str, str, str]]]] = {
    "dashboard": ("Dashboard", [
        ("kpis", "Summary cards", "full"),
        ("flow", "Flow at a glance", "full"),
        ("projects", "Projects", "large"),
        ("waiting", "Waiting for you", "small"),
        ("building", "What is being built", "large"),
        ("usage", "AI usage", "large"),
        ("tokens", "Tokens and cost", "small"),
        ("quota", "AI account quota", "full"),
        ("tool-waits", "Tickets waiting for a tool", "full"),
        ("autonomy", "Master autonomy", "full"),
        ("escalations", "Escalations and owner decisions", "full"),
    ]),
    "review": ("Human review", [
        ("queue", "Human review queue", "full"),
        ("metrics", "People needed", "full"),
        ("items", "Waiting tickets", "full"),
        ("threads", "AI question threads", "full"),
    ]),
    "machines": ("Machines and AI", [
        ("machines", "Machines and AI", "full"),
        ("add-machine", "Add a machine", "full"),
    ]),
    "knowledge": ("Knowledge", [
        ("header", "Knowledge and Skills", "full"),
        ("resources", "Resources", "medium"),
        ("editor", "Create or edit a resource", "medium"),
        ("history", "History", "full"),
        ("approvals", "Policy approvals", "medium"),
        ("learning", "Learning and study cases", "full"),
    ]),
    "delivery": ("Delivery", [
        ("kpis", "Project summary", "full"),
        ("epics", "Epics", "full"),
        ("board", "Board", "full"),
        ("attention", "Needs attention", "large"),
        ("runs", "Recent runs", "full"),
        ("analytics", "Analytics", "full"),
    ]),
    "users": ("Users and roles", [
        ("users", "Console users", "large"),
        ("access", "Your console access", "medium"),
    ]),
}

# Default layouts by profile. For each page: "order" lists the blocks that come first (the others follow in the
# canonical order), "hidden" are hidden by default and "sizes" overrides the canonical sizes.
PROFILES: dict[str, dict[str, dict[str, Any]]] = {
    # Cost and quota first, followed by the work summary.
    "owner": {
        "dashboard": {"order": ["tokens", "quota", "usage", "kpis", "flow", "projects", "building", "waiting"]},
        "delivery": {"order": ["kpis", "attention", "board", "epics", "runs", "analytics"]},
    },
    # The board and the people who are blocked first.
    "lead": {
        "dashboard": {"order": ["kpis", "flow", "waiting", "building", "projects"], "hidden": ["autonomy"],
                      "sizes": {"waiting": "medium", "building": "large"}},
        "delivery": {"order": ["board", "attention", "kpis", "epics", "runs"], "hidden": ["analytics"]},
        "review": {"order": ["queue", "items", "metrics", "threads"]},
    },
    "business": {
        "dashboard": {"order": ["kpis", "flow", "projects", "building", "waiting"],
                      "hidden": ["quota", "tool-waits", "autonomy"]},
        "delivery": {"order": ["kpis", "epics", "board", "attention"], "hidden": ["runs", "analytics"]},
    },
    # Health of machines, accounts and quota first.
    "sa": {
        "dashboard": {"order": ["quota", "tool-waits", "usage", "tokens", "kpis", "flow"],
                      "hidden": ["projects", "building"]},
        "delivery": {"order": ["runs", "analytics", "attention", "kpis"], "hidden": ["epics"]},
    },
    # The reviews waiting for a person first.
    "reviewer": {
        "dashboard": {"order": ["waiting", "kpis", "building", "flow"],
                      "hidden": ["quota", "tool-waits", "autonomy"],
                      "sizes": {"waiting": "large"}},
        "review": {"order": ["items", "queue", "threads", "metrics"], "hidden": ["metrics"]},
    },
}
# Existing console role (console_auth.ROLE_PERMISSIONS) -> profile. The other names are the roles the ticket names;
# they take effect when a role of that name is added to ROLE_PERMISSIONS.
ROLE_PROFILE: dict[str, str] = {
    "administrator": "owner", "super_admin": "owner", "owner": "owner", "product_owner": "business",
    "lead": "lead", "team_lead": "lead", "delivery": "lead",
    "business": "business", "ba": "business", "sa": "sa", "stakeholder": "business",
    "human_reviewer": "reviewer", "reviewer": "reviewer",
}


class LayoutBlock(BaseModel):
    id: str = Field(max_length=64)
    size: str = Field(max_length=16)
    hidden: bool = False


class LayoutBody(BaseModel):
    blocks: list[LayoutBlock] = Field(max_length=MAX_BLOCKS)


class LayoutError(ValueError):
    """The request names an unknown page or a size that does not exist."""


def page_ids(page: str) -> list[str]:
    return [b[0] for b in _blocks(page)]


def _blocks(page: str) -> list[tuple[str, str, str]]:
    if page not in PAGES:
        raise LayoutError("unknown page")
    return PAGES[page][1]


def profile_for(role: str) -> str:
    return ROLE_PROFILE.get(role, "")


def default_layout(page: str, role: str) -> list[dict]:
    """The default layout of the person's role for a page: [{id, size, hidden}] in display order."""
    blocks = _blocks(page)
    spec = PROFILES.get(profile_for(role), {}).get(page, {})
    known = {b[0]: b for b in blocks}
    requested = [i for i in spec.get("order", []) if i in known]
    order = requested + [b[0] for b in blocks if b[0] not in requested]
    hidden = set(spec.get("hidden", []))
    sizes = spec.get("sizes", {})
    return [{"id": i, "size": sizes.get(i, known[i][2]), "hidden": i in hidden} for i in order]


def resolve(page: str, role: str, saved: Any) -> list[dict]:
    """Merge a saved layout with the blocks that exist now (see the module docstring)."""
    default = default_layout(page, role)
    if not isinstance(saved, list):
        return default
    by_default = {item["id"]: item for item in default}
    out: list[dict] = []
    seen: set[str] = set()
    for item in saved:
        if not isinstance(item, dict):
            continue
        block = item.get("id")
        if not isinstance(block, str) or block not in by_default or block in seen:
            continue  # a block that no longer exists (or a repeat) is ignored
        size = item.get("size")
        out.append({"id": block, "size": size if size in SIZES else by_default[block]["size"],
                    "hidden": item.get("hidden") is True})
        seen.add(block)
    for item in default:  # blocks added by a later release go to the end, visible
        if item["id"] not in seen:
            out.append({"id": item["id"], "size": item["size"], "hidden": False})
    return out


def normalise(page: str, blocks: Any) -> list[dict]:
    """Validate a layout sent by the console. Unknown blocks are dropped; a wrong size is refused."""
    _blocks(page)
    if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
        raise LayoutError("blocks must be a list")
    known = set(page_ids(page))
    out: list[dict] = []
    seen: set[str] = set()
    for item in blocks:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise LayoutError("every block needs an id")
        if item["id"] not in known or item["id"] in seen:
            continue
        if item.get("size") not in SIZES:
            raise LayoutError("size must be one of " + ", ".join(SIZES))
        seen.add(item["id"])
        out.append({"id": item["id"], "size": item["size"], "hidden": item.get("hidden") is True})
    return out


def page_view(page: str, role: str, saved: Any) -> dict:
    titles = {b[0]: b[1] for b in _blocks(page)}
    blocks = [{**item, "title": titles[item["id"]]} for item in resolve(page, role, saved)]
    return {"page": page, "title": PAGES[page][0], "blocks": blocks, "customized": isinstance(saved, list)}


def load_saved(db, person: str) -> dict[str, Any]:
    rows = db.all("SELECT page, blocks FROM console_layouts WHERE email=%s", (person,))
    return {r["page"]: r["blocks"] if not isinstance(r["blocks"], str) else json.loads(r["blocks"]) for r in rows}


def save(db, person: str, page: str, blocks: list[dict]) -> None:
    db.run("INSERT INTO console_layouts (email, page, blocks) VALUES (%s, %s, %s::jsonb)"
           " ON CONFLICT (email, page) DO UPDATE SET blocks=EXCLUDED.blocks, updated_at=now()",
           (person, page, json.dumps(blocks)))


def reset(db, person: str, page: str) -> None:
    db.run("DELETE FROM console_layouts WHERE email=%s AND page=%s", (person, page))


def overview(db, person: str, role: str) -> dict:
    saved = load_saved(db, person)
    return {"person": person, "role": role, "profile": profile_for(role) or "default", "sizes": list(SIZES),
            "pages": {page: page_view(page, role, saved.get(page)) for page in PAGES}}


def roles_without_profile() -> list[str]:
    """Console roles that have no role default yet (they get the canonical layout); used by the tests."""
    return sorted(r for r in ROLE_PERMISSIONS if r not in ROLE_PROFILE)
