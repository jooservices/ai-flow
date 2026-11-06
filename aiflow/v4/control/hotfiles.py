"""Hot-file scheduling: predict the files a ticket will touch and never start two tickets that share a hot file.

Most tickets touch the same few files (the console page, the shared test fixtures, the control API). Two tickets
that change the same hot file at the same time end in a merge conflict, another development run and a new
verification. Before a dev run is offered, `predict` builds the ticket's footprint from three sources:

  ticket   file paths named in the summary and description, plus keyword areas (console, dashboard, ...)
  epic     the same for the epic text, since sibling tickets usually work in the same area
  history  files that past dev runs of the same epic actually changed (runs.details.changed_files)

Only *hot* files count: a path that matches HOT_FILES. `blocking_ticket` tells whether a candidate would share a
hot file with work already in flight; the candidate is then not offered (the account gets another ticket, or
waits) until the other ticket releases its reservation. Waiting never permits two active tickets to share a hot
file. docs/v4/merge-conflicts.md describes the rules.
"""
from __future__ import annotations

import fnmatch
import re
from datetime import datetime
from typing import Any, Callable, Iterable

from .features.tickets.repository import TicketRepository

# Files that many tickets change at once. Entries are fnmatch globs on the repository path.
HOT_FILES: tuple[str, ...] = (
    # Oversized files present in the develop baseline (JAIF-273). The old-flow paths stay covered without changing
    # those files; the v4 giant-file check reports their baseline size and prevents further growth.
    "aiflow/supervisor.py",
    "aiflow/v4/control/ui/console.html",  # the console page as tickets name it; its sources are the next line
    "aiflow/v4/control/ui/console/*",
    "aiflow/v4/control/api.py",
    "aiflow/v4/worker/containers.py",
    "tests/test_supervisor.py",
    "tests/v4/test_console.py",
    # Shared fixtures and test suites are common coordination points across otherwise separate feature tickets.
    "tests/v4/conftest.py",
    "tests/v4/fixtures/*",
    "tests/v4/test_*.py",
    "tests/v4/*_suite.cjs",
    # A node-agent change can affect every project served by a machine.
    "aiflow/v4/node/agent.py",
    "pyproject.toml",
    "uv.lock",
    "CHANGELOG.md",
    # Console components loaded alongside the oversized page share its scheduling reservation.
    "aiflow/v4/control/ui/licensing_notice.js",
)

_CONSOLE = (
    "Command Center console page",
    ("aiflow/v4/control/ui/console.html", "aiflow/v4/control/ui/licensing_notice.js"),
)
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

PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|html|js|cjs|md|sql|yml|yaml|toml|lock|jsonl|json|sh|css|txt))\b")
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


def _past_files(db, ticket: dict[str, Any], epic: str, ticket_repository: TicketRepository | None = None) -> list[str]:
    """Files changed by the latest dev runs of the same epic (the ticket's own earlier runs included)."""
    tickets = ticket_repository or TicketRepository(db)
    rows = tickets.hotfile_past_files(ticket["key"], epic, HISTORY_RUNS)
    files: list[str] = []
    for row in rows:
        files += [f for f in (row["files"] or []) if isinstance(f, str)]
    return list(dict.fromkeys(files))


def predict(db, ticket: dict[str, Any], epic: str = "", hot: Iterable[str] = HOT_FILES, *,
            include_history: bool = True, ticket_repository: TicketRepository | None = None) -> dict[str, Any]:
    """The footprint of a ticket: {"files", "areas", "hot", "sources"}. `hot` is the part that can conflict."""
    tickets = ticket_repository or TicketRepository(db)
    epic_row = tickets.hotfile_epic(epic) if epic else None
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
    if include_history:
        for path in _past_files(db, ticket, epic, tickets):
            files.setdefault(path, "history")
    hot_files = sorted(p for p in files if is_hot(p, hot))
    return {"files": sorted(files), "areas": sorted(areas), "hot": hot_files,
            "sources": {p: files[p] for p in hot_files}}


def blocking_ticket(footprint: dict[str, Any], in_flight: dict[str, Iterable[str]],
                    slots: int = 1) -> tuple[str, list[str]] | None:
    """(key, shared hot files) of the first ticket in flight that blocks the footprint.

    A hot file takes `slots` tickets at the same time (default one); it blocks only when that many are in flight."""
    mine = set(footprint.get("hot") or [])
    full = {path for path in mine if sum(path in set(files) for files in in_flight.values()) >= max(1, slots)}
    for key in sorted(in_flight):
        shared = sorted(full & set(in_flight[key]))
        if shared:
            return key, shared
    return None


def in_flight_hot_files(db, footprint_of: Callable[[dict], dict], exclude: str = "", *,
                        ticket_repository: TicketRepository | None = None) -> dict[str, list[str]]:
    """Hot files of tickets that are being worked on: a running development run, or waiting in verification or review.

    A ticket sent back for rework that still has an open pull request holds nothing while it waits: it updates its
    branch when it resumes. Counting it deadlocked the flow (thirteen waiting tickets, each blocked by the open pull
    requests of the others, and nothing could start)."""
    tickets = ticket_repository or TicketRepository(db)
    rows = tickets.hotfile_in_flight(exclude)
    return {t["key"]: footprint_of(t)["hot"] for t in rows}


def mark_blocked(db, ticket_key: str) -> datetime:
    """When the ticket was first held back for a hot file; the first call records it, later calls keep it."""
    db.run("INSERT INTO hot_file_blocked (ticket_key) VALUES (%s) ON CONFLICT (ticket_key) DO NOTHING", (ticket_key,))
    return db.one("SELECT blocked_at FROM hot_file_blocked WHERE ticket_key=%s", (ticket_key,))["blocked_at"]


def clear_blocked(db, ticket_key: str) -> None:
    """The ticket is not held back any more: a later block starts a new wait."""
    db.run("DELETE FROM hot_file_blocked WHERE ticket_key=%s", (ticket_key,))


RESERVATION_GRACE_S = 120  # a claim inserts its running run row right after it reserved


def reserve(db, ticket_key: str, run_id: str, hot: Iterable[str], slots: int = 1, *,
            ticket_repository: TicketRepository | None = None) -> tuple[str, list[str]] | None:
    """Reserve the hot files of a claimed dev run, atomically with the claim.

    Everything runs in one transaction under an advisory lock, so of two simultaneous claims only the first gets a
    shared file. Returns (holder ticket, shared files) when another ticket holds one, else None and the files are
    reserved. A reservation is stale (and dropped here) when its run is not running any more and its ticket is not
    waiting in verify or human review. Time spent waiting never overrides a live reservation."""
    paths = sorted(set(hot))
    if not paths:
        return None
    tickets = ticket_repository or TicketRepository(db)
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('aiflow4-hot-file-reservations'))")
            tickets.cleanup_hot_file_reservations(conn, RESERVATION_GRACE_S)
            rows = conn.execute("SELECT path, ticket_key FROM hot_file_reservations"
                                " WHERE path = ANY(%s) AND ticket_key <> %s ORDER BY path, reserved_at",
                                (paths, ticket_key)).fetchall()
            holders: dict[str, list[str]] = {}
            for r in rows:
                holders.setdefault(r["path"], []).append(r["ticket_key"])
            full = {path: keys for path, keys in holders.items() if len(keys) >= max(1, slots)}
            if full:
                first = full[min(full)][0]
                return first, sorted(path for path, keys in full.items() if first in keys)
            for path in paths:
                conn.execute(
                    "INSERT INTO hot_file_reservations (path, ticket_key, run_id) VALUES (%s,%s,%s)"
                    " ON CONFLICT (path, ticket_key) DO UPDATE SET run_id=EXCLUDED.run_id, reserved_at=now()",
                    (path, ticket_key, run_id))
    return None


def unreserve(db, run_id: str) -> None:
    db.run("DELETE FROM hot_file_reservations WHERE run_id=%s", (run_id,))
