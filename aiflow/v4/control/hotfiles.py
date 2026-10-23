"""Hot-file scheduling: predict the files a ticket will touch and never start two tickets that share a hot file.

Most tickets touch the same few files (the console page, the shared test fixtures, the control API). Two tickets
that change the same hot file at the same time end in a merge conflict, another development run and a new
verification. Before a dev run is offered, `predict` builds the ticket's footprint from three sources:

  ticket   file paths named in the summary and description, plus keyword areas (console, dashboard, ...)
  epic     the same for the epic text, since sibling tickets usually work in the same area
  history  files that past dev runs of the same epic actually changed (runs.details.changed_files)

Only *hot* files count: a path that matches HOT_FILES. `blocking_ticket` tells whether a candidate would share a
hot file with work already in flight; the candidate is then not offered (the account gets another ticket, or
waits) until the other ticket merges. A ticket that waited longer than MAX_WAIT is released anyway, so a hot
file can never starve a ticket. docs/v4/merge-conflicts.md describes the rules.
"""
from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

# Files that many tickets change at once. Entries are fnmatch globs on the repository path.
HOT_FILES: tuple[str, ...] = (
    "aiflow/v4/control/ui/console.html",
    "aiflow/v4/control/api.py",
    "tests/v4/conftest.py",
    "pyproject.toml",
    "uv.lock",
    "CHANGELOG.md",
)

_CONSOLE = ("Command Center console page", ("aiflow/v4/control/ui/console.html",))
# Words in a ticket that point to a hot file even when no path is written down.
AREAS: dict[str, tuple[str, tuple[str, ...]]] = {
    "console": _CONSOLE,
    "command center": _CONSOLE,
    "dashboard": _CONSOLE,
    "control api": ("control API", ("aiflow/v4/control/api.py",)),
    "endpoint": ("control API", ("aiflow/v4/control/api.py",)),
    "dependenc": ("dependencies", ("pyproject.toml", "uv.lock")),
    "changelog": ("changelog", ("CHANGELOG.md",)),
}

PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|html|js|cjs|md|sql|yml|yaml|toml|lock|json|sh|css|txt))\b")
MAX_WAIT = timedelta(hours=6)
HISTORY_RUNS = 8


def is_hot(path: str, hot: Iterable[str] = HOT_FILES) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in hot)


def paths_in(text: str) -> list[str]:
    """File paths written in a text, in order, without duplicates."""
    return list(dict.fromkeys(m.group(1) for m in PATH.finditer(text or "")))


def areas_in(text: str) -> dict[str, list[str]]:
    """Keyword areas found in a text -> the hot files they point to."""
    low = (text or "").lower()
    out: dict[str, list[str]] = {}
    for word, (area, files) in AREAS.items():
        if re.search(rf"\b{re.escape(word)}", low):
            known = out.setdefault(area, [])
            known += [f for f in files if f not in known]
    return out


def _past_files(db, ticket: dict[str, Any], epic: str) -> list[str]:
    """Files changed by the latest dev runs of the same epic (the ticket's own earlier runs included)."""
    rows = db.all(
        "SELECT r.details->'changed_files' AS files FROM runs r JOIN tickets t ON t.key = r.ticket_key"
        " WHERE r.step = 'dev' AND r.outcome = 'done' AND r.details ? 'changed_files'"
        " AND (r.ticket_key = %s OR (%s <> '' AND t.parent_key = %s))"
        " ORDER BY r.started_at DESC LIMIT %s", (ticket["key"], epic, epic, HISTORY_RUNS))
    files: list[str] = []
    for row in rows:
        files += [f for f in (row["files"] or []) if isinstance(f, str)]
    return list(dict.fromkeys(files))


def predict(db, ticket: dict[str, Any], epic: str = "", hot: Iterable[str] = HOT_FILES) -> dict[str, Any]:
    """The footprint of a ticket: {"files", "areas", "hot", "sources"}. `hot` is the part that can conflict."""
    epic_row = db.one("SELECT summary, description FROM tickets WHERE key=%s", (epic,)) if epic else None
    texts = (("ticket", f"{ticket.get('summary', '')}\n{ticket.get('description', '')}"),
             ("epic", f"{epic_row['summary']}\n{epic_row['description']}" if epic_row else ""))
    files: dict[str, str] = {}
    areas: set[str] = set()
    for source, body in texts:
        for path in paths_in(body):
            files.setdefault(path, source)
        for area, area_files in areas_in(body).items():
            areas.add(area)
            for path in area_files:
                files.setdefault(path, source)
    for path in _past_files(db, ticket, epic):
        files.setdefault(path, "history")
    hot_files = sorted(p for p in files if is_hot(p, hot))
    return {"files": sorted(files), "areas": sorted(areas), "hot": hot_files,
            "sources": {p: files[p] for p in hot_files}}


def blocking_ticket(footprint: dict[str, Any], in_flight: dict[str, Iterable[str]]) -> tuple[str, list[str]] | None:
    """(key, shared hot files) of the first ticket in flight that shares a hot file with the footprint."""
    mine = set(footprint.get("hot") or [])
    for key in sorted(in_flight):
        shared = sorted(mine & set(in_flight[key]))
        if shared:
            return key, shared
    return None


def in_flight_hot_files(db, footprint_of: Callable[[dict], dict], exclude: str = "") -> dict[str, list[str]]:
    """Hot files of the tickets with a running dev run or an open pull request that has not merged yet."""
    rows = db.all(
        "SELECT * FROM tickets WHERE key <> %s AND (status IN ('In Verify', 'Human review') OR EXISTS ("
        " SELECT 1 FROM runs r WHERE r.ticket_key = tickets.key AND r.step = 'dev' AND r.status = 'running'))",
        (exclude,))
    return {t["key"]: footprint_of(t)["hot"] for t in rows}


def released_by_wait(ready_since: datetime | None, now: datetime | None = None) -> bool:
    """A ticket that waited longer than MAX_WAIT for a hot file starts anyway."""
    if ready_since is None:
        return False
    now = now or datetime.now(timezone.utc)
    if ready_since.tzinfo is None:
        ready_since = ready_since.replace(tzinfo=timezone.utc)
    return now - ready_since > MAX_WAIT
